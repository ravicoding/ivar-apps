#!/usr/bin/env bash
# Puts the web app on your own domain (HTTPS), e.g. marketpulse.ivarsolutions.net
# Run AFTER ./deploy.sh, with the same AWS_REGION / STACK_NAME.
#
#   ./setup-domain.sh marketpulse.ivarsolutions.net
#
# If ivarsolutions.net is a Route 53 hosted zone in this account, DNS records are created
# automatically. Otherwise the script prints the two DNS records to add at your DNS provider.
# Safe to re-run: it reuses the certificate, domain and mapping if they already exist.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${1:-marketpulse.ivarsolutions.net}"
STACK="${STACK_NAME:-stock-news}"
REGION="${AWS_REGION:-$(aws configure get region || true)}"
REGION="${REGION:-us-east-1}"
PARENT="${DOMAIN#*.}"   # ivarsolutions.net

echo "==> Domain $DOMAIN, region $REGION, stack $STACK"

STATUS=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || true)
if [ -z "$STATUS" ]; then
  echo
  echo "The app (stack '$STACK') isn't deployed in $REGION yet."
  echo "Looking for it in other regions…"
  FOUND=""
  for r in $(aws ec2 describe-regions --query 'Regions[].RegionName' --output text 2>/dev/null); do
    s=$(aws cloudformation describe-stacks --region "$r" --stack-name "$STACK" \
      --query "Stacks[0].StackStatus" --output text 2>/dev/null || true)
    [ -n "$s" ] && { echo "    found in $r ($s)"; FOUND="$r"; }
  done
  if [ -n "$FOUND" ]; then
    echo "Run:  AWS_REGION=$FOUND ./setup-domain.sh $DOMAIN"
  else
    echo "Not found anywhere. Run ./deploy.sh first (see DEPLOY.md steps 2-3), then this script."
  fi
  exit 1
fi
case "$STATUS" in
  CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE) ;;
  *) echo "The stack is in state $STATUS, not ready. Re-run ./deploy.sh and check its output for errors."; exit 1 ;;
esac

API_ID=$(aws cloudformation describe-stack-resource --region "$REGION" --stack-name "$STACK" \
  --logical-resource-id ServerlessHttpApi --query StackResourceDetail.PhysicalResourceId --output text)
echo "    HTTP API id: $API_ID"

# Route 53 hosted zone for the parent domain (if any)
ZONE_ID=$(aws route53 list-hosted-zones-by-name --dns-name "$PARENT" \
  --query "HostedZones[?Name=='${PARENT}.' && Config.PrivateZone==\`false\`].Id | [0]" --output text 2>/dev/null || true)
[ "$ZONE_ID" = "None" ] && ZONE_ID=""
ZONE_ID="${ZONE_ID#/hostedzone/}"
if [ -n "$ZONE_ID" ]; then echo "    Route 53 zone found: $ZONE_ID (DNS will be automatic)"
else echo "    No Route 53 zone for $PARENT in this account: you'll add DNS records yourself"; fi

upsert() {  # name type value
  aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" --change-batch \
    "{\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"$1\",\"Type\":\"$2\",\"TTL\":300,\"ResourceRecords\":[{\"Value\":\"$3\"}]}}]}" >/dev/null
}

# 1. TLS certificate (must be in the same region as the API)
CERT_ARN=$(aws acm list-certificates --region "$REGION" --certificate-statuses PENDING_VALIDATION ISSUED \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn | [0]" --output text)
if [ "$CERT_ARN" = "None" ] || [ -z "$CERT_ARN" ]; then
  echo "==> Requesting certificate"
  CERT_ARN=$(aws acm request-certificate --region "$REGION" --domain-name "$DOMAIN" \
    --validation-method DNS --query CertificateArn --output text)
fi
echo "    Certificate: $CERT_ARN"

STATUS=$(aws acm describe-certificate --region "$REGION" --certificate-arn "$CERT_ARN" --query Certificate.Status --output text)
if [ "$STATUS" != "ISSUED" ]; then
  for _ in $(seq 1 20); do  # validation record takes a few seconds to appear
    VNAME=$(aws acm describe-certificate --region "$REGION" --certificate-arn "$CERT_ARN" \
      --query "Certificate.DomainValidationOptions[0].ResourceRecord.Name" --output text)
    [ "$VNAME" != "None" ] && break; sleep 3
  done
  VVALUE=$(aws acm describe-certificate --region "$REGION" --certificate-arn "$CERT_ARN" \
    --query "Certificate.DomainValidationOptions[0].ResourceRecord.Value" --output text)
  if [ -n "$ZONE_ID" ]; then
    echo "==> Adding certificate validation record in Route 53"
    upsert "$VNAME" CNAME "$VVALUE"
  else
    echo
    echo "ACTION NEEDED: add this DNS record at your DNS provider for $PARENT, then press Enter:"
    echo "    Type:  CNAME"
    echo "    Name:  $VNAME"
    echo "    Value: $VVALUE"
    read -r _
  fi
  echo "==> Waiting for the certificate to be issued (usually 1-10 minutes)…"
  aws acm wait certificate-validated --region "$REGION" --certificate-arn "$CERT_ARN"
fi
echo "    Certificate issued"

# 2. API Gateway custom domain
if ! aws apigatewayv2 get-domain-name --region "$REGION" --domain-name "$DOMAIN" >/dev/null 2>&1; then
  echo "==> Creating API Gateway custom domain"
  aws apigatewayv2 create-domain-name --region "$REGION" --domain-name "$DOMAIN" \
    --domain-name-configurations CertificateArn="$CERT_ARN",EndpointType=REGIONAL,SecurityPolicy=TLS_1_2 >/dev/null
fi
TARGET=$(aws apigatewayv2 get-domain-name --region "$REGION" --domain-name "$DOMAIN" \
  --query "DomainNameConfigurations[0].ApiGatewayDomainName" --output text)
TARGET_ZONE=$(aws apigatewayv2 get-domain-name --region "$REGION" --domain-name "$DOMAIN" \
  --query "DomainNameConfigurations[0].HostedZoneId" --output text)

# 3. Map the domain to the API
MAPPED=$(aws apigatewayv2 get-api-mappings --region "$REGION" --domain-name "$DOMAIN" \
  --query "Items[?ApiId=='$API_ID'] | length(@)" --output text)
if [ "$MAPPED" = "0" ]; then
  echo "==> Mapping $DOMAIN to the API"
  aws apigatewayv2 create-api-mapping --region "$REGION" --domain-name "$DOMAIN" \
    --api-id "$API_ID" --stage '$default' >/dev/null
fi

# 4. Point the domain at API Gateway
if [ -n "$ZONE_ID" ]; then
  echo "==> Creating Route 53 alias record"
  aws route53 change-resource-record-sets --hosted-zone-id "$ZONE_ID" --change-batch \
    "{\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"$DOMAIN\",\"Type\":\"A\",\"AliasTarget\":{\"DNSName\":\"$TARGET\",\"HostedZoneId\":\"$TARGET_ZONE\",\"EvaluateTargetHealth\":false}}}]}" >/dev/null
else
  echo
  echo "ACTION NEEDED: add this DNS record at your DNS provider for $PARENT:"
  echo "    Type:  CNAME"
  echo "    Name:  ${DOMAIN%%.$PARENT}   (i.e. $DOMAIN)"
  echo "    Value: $TARGET"
fi

echo
echo "Done. In a few minutes (DNS propagation) open:  https://$DOMAIN"
[ -f .access_key ] && echo "Access key: $(cat .access_key)"
