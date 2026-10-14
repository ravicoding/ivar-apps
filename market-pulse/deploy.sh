#!/usr/bin/env bash
# Deploys the stack using only the AWS CLI (no SAM CLI needed).
#
#   export FINNHUB_API_KEY=xxxxxxxx      # news + earnings (free: finnhub.io)
#   export FRED_API_KEY=xxxxxxxx         # CPI, payrolls, etc. (free: fredaccount.stlouisfed.org)
#   ./deploy.sh
#   BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 ./deploy.sh   # + AI digests
#
# Optional env: STACK_NAME, AWS_REGION, SYMBOLS, BEDROCK_MODEL_ID, DIGEST_HOURS, ACCESS_KEY
set -euo pipefail
cd "$(dirname "$0")"

: "${FINNHUB_API_KEY:?Set FINNHUB_API_KEY first (free key at https://finnhub.io)}"
FRED_API_KEY="${FRED_API_KEY:-}"
STACK="${STACK_NAME:-stock-news}"
REGION="${AWS_REGION:-$(aws configure get region || true)}"
REGION="${REGION:-us-east-1}"
SYMBOLS="${SYMBOLS:-AAPL,MSFT,NVDA,AMZN,GOOGL}"
BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-}"
DIGEST_HOURS="${DIGEST_HOURS:-8,12,17}"

# Access key for the web page: reuse the saved one, or create one on first deploy.
if [ -z "${ACCESS_KEY:-}" ]; then
  if [ -f .access_key ]; then ACCESS_KEY=$(cat .access_key)
  else
    ACCESS_KEY=$(openssl rand -hex 12 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(12))')
    [ -n "$ACCESS_KEY" ] || { echo "Could not generate an access key; set ACCESS_KEY=yourpassword and re-run."; exit 1; }
    echo "$ACCESS_KEY" > .access_key; chmod 600 .access_key
  fi
fi
[ -z "$FRED_API_KEY" ] && echo "Note: FRED_API_KEY not set, so the economic calendar (CPI, payrolls…) will be empty."

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${STACK}-artifacts-${ACCOUNT}-${REGION}"

echo "==> Region $REGION, account $ACCOUNT, stack $STACK"

if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" 2>/dev/null; then
  echo "==> Creating artifact bucket $BUCKET"
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
fi

echo "==> Packaging"
aws cloudformation package --region "$REGION" \
  --template-file template.yaml --s3-bucket "$BUCKET" \
  --output-template-file packaged.yaml >/dev/null

echo "==> Deploying (takes 1-3 minutes)"
aws cloudformation deploy --region "$REGION" \
  --stack-name "$STACK" --template-file packaged.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides \
    FinnhubApiKey="$FINNHUB_API_KEY" \
    FredApiKey="$FRED_API_KEY" \
    AccessKey="$ACCESS_KEY" \
    Symbols="$SYMBOLS" \
    BedrockModelId="$BEDROCK_MODEL_ID" \
    DigestHours="$DIGEST_HOURS"

out() { aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
FN=$(out FunctionName); URL=$(out WebUrl)

echo "==> Running the collector once now to load today's data"
aws lambda invoke --region "$REGION" --function-name "$FN" \
  --cli-binary-format raw-in-base64-out --payload '{}' /tmp/stock-news-run.json >/dev/null
cat /tmp/stock-news-run.json; echo

echo
echo "Done."
echo "  Web app:    $URL"
echo "  Access key: $ACCESS_KEY   (also saved in .access_key)"
