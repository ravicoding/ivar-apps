#!/usr/bin/env bash
# Show what was collected today (or for a date: ./today.sh 2026-10-12).
#   ./today.sh               news, events and digests for today
#   ./today.sh 2026-10-09    a past day
set -euo pipefail
STACK="${STACK_NAME:-stock-news}"
REGION="${AWS_REGION:-$(aws configure get region || true)}"
REGION="${REGION:-us-east-1}"
DAY="${1:-$(TZ=America/New_York date +%F)}"

TABLE=$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='TableName'].OutputValue" --output text)

q() {  # $1 = partition key, $2 = JMESPath projection
  aws dynamodb query --region "$REGION" --table-name "$TABLE" \
    --key-condition-expression "pk = :p" \
    --expression-attribute-values "{\":p\":{\"S\":\"$1\"}}" \
    --no-scan-index-forward --query "$2" --output table
}

echo "===== AI digests ($DAY) ====="
q "DIGEST#$DAY" 'Items[].{time:sk.S,digest:summary.S}'
echo "===== Economic releases (next 14 days) ====="
END=$(date -d "$DAY +14 days" +%F 2>/dev/null || date -v+14d -j -f %F "$DAY" +%F)
aws dynamodb query --region "$REGION" --table-name "$TABLE" \
  --key-condition-expression "pk = :p AND sk BETWEEN :a AND :b" \
  --expression-attribute-values "{\":p\":{\"S\":\"ECONCAL\"},\":a\":{\"S\":\"$DAY\"},\":b\":{\"S\":\"$END#~\"}}" \
  --query 'Items[].{date:date.S,time:time_et.S,release:summary.S}' --output table
echo "===== Earnings & IPOs ($DAY) ====="
q "EVENT#$DAY" 'Items[].{type:type.S,event:summary.S}'
echo "===== News ($DAY, newest first) ====="
q "NEWS#$DAY" 'Items[].{time:published_at.S,source:source.S,headline:headline.S,summary:summary.S}'
