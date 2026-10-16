#!/usr/bin/env bash
# Upload the settings page (and its phone app files) to S3, pointed at the deployed Lambda.
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
# Phone app files: the service worker must never be cached long, or updates won't reach phones.
aws s3 cp web/sw.js "${DEST}sw.js" --content-type "application/javascript" --cache-control "no-cache"
aws s3 cp web/manifest.webmanifest "${DEST}manifest.webmanifest" \
  --content-type "application/manifest+json" --cache-control "max-age=3600"
for icon in icon-192.png icon-512.png apple-touch-icon.png; do
  aws s3 cp "web/$icon" "${DEST}$icon" --content-type "image/png" --cache-control "max-age=86400"
done

echo
echo "Uploaded to $DEST"
echo "API: $API_URL"
echo "Open: https://$BUCKET/${FOLDER:+$FOLDER/}"
echo "(If you serve the site through CloudFront, invalidate /${FOLDER:+$FOLDER/}* to see changes right away.)"
