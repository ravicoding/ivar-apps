#!/usr/bin/env bash
# Upload the settings page to S3, pointed at the deployed Lambda.
#
# Usage:  ./deploy-web.sh [bucket] [folder] [stack-name]
# Default: ./deploy-web.sh ivarsolutions.net alerts price-alerts
#   -> page lives at https://ivarsolutions.net/alerts/
set -euo pipefail

BUCKET="${1:-ivarsolutions.net}"
FOLDER="${2:-alerts}"
STACK="${3:-price-alerts}"

cd "$(dirname "$0")"

API_URL=$(aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)
if [[ -z "$API_URL" || "$API_URL" == "None" ]]; then
  echo "Couldn't find the ApiUrl output on stack '$STACK'. Run 'sam deploy' first." >&2
  exit 1
fi

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
cp web/index.html "$OUT/index.html"
printf 'window.PRICE_ALERTS_API = "%s";\n' "$API_URL" > "$OUT/config.js"

DEST="s3://$BUCKET/${FOLDER:+$FOLDER/}"
aws s3 cp "$OUT/index.html" "${DEST}index.html" \
  --content-type "text/html; charset=utf-8" --cache-control "no-cache"
aws s3 cp "$OUT/config.js" "${DEST}config.js" \
  --content-type "application/javascript" --cache-control "no-cache"

echo
echo "Uploaded to $DEST"
echo "API: $API_URL"
echo "Open: https://$BUCKET/${FOLDER:+$FOLDER/}"
echo "(If you serve the site through CloudFront, invalidate /${FOLDER:+$FOLDER/}* to see changes right away.)"
