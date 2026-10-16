#!/usr/bin/env bash
# Build and deploy the Lambda stack. Bundles the `cryptography` package (for web push)
# as Linux wheels for the Lambda runtime, so it works from a Mac without Docker or SAM.
#
# Usage:  ./deploy.sh [deploy-bucket] [stack-name] [extra --parameter-overrides...]
# Example (first deploy): ./deploy.sh my-bucket price-alerts AppPassword=... PhoneNumber=+15551234567
# Later deploys keep the existing parameter values: ./deploy.sh my-bucket
set -euo pipefail

BUCKET="${1:?Usage: ./deploy.sh <deploy-bucket> [stack-name] [Param=Value ...]}"
STACK="${2:-price-alerts}"
shift $(( $# >= 2 ? 2 : 1 ))

cd "$(dirname "$0")"
BUILD=.build
rm -rf "$BUILD" && mkdir -p "$BUILD/src"
cp src/*.py "$BUILD/src/"
python3 -m pip install --quiet --disable-pip-version-check -r src/requirements.txt --target "$BUILD/src" \
  --platform manylinux2014_x86_64 --implementation cp --python-version 3.12 --only-binary=:all:
cp template.yaml "$BUILD/template.yaml"

# Create the VAPID key for phone app notifications once (kept in SSM, never in the repo).
PARAM=/price-alerts/vapid-private-key
if ! aws ssm get-parameter --name "$PARAM" >/dev/null 2>&1; then
  python3 -m pip install --quiet --disable-pip-version-check --target "$BUILD/keygen" cryptography
  KEY=$(PYTHONPATH="$BUILD/keygen:src" python3 -c 'import webpush; print(webpush.new_vapid_key()[0])')
  aws ssm put-parameter --name "$PARAM" --type SecureString --value "$KEY" \
    --description "VAPID private key for Price Alerts web push" >/dev/null
  echo "Created $PARAM"
fi

aws cloudformation package --template-file "$BUILD/template.yaml" --s3-bucket "$BUCKET" \
  --s3-prefix "$STACK" --output-template-file "$BUILD/packaged.yaml" >/dev/null
aws cloudformation deploy --template-file "$BUILD/packaged.yaml" --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND ${1:+--parameter-overrides "$@"}
