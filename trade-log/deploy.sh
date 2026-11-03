#!/usr/bin/env bash
# Package and deploy Trade Log (DynamoDB table, Lambda, HTTP API, log.ivarsolutions.net domain).
# No extra Python packages are needed: boto3 is part of the Lambda runtime.
#
# Usage:  ./deploy.sh <deploy-bucket> [stack-name] [Param=Value ...]
# First deploy:  ./deploy.sh my-bucket trade-log AllowedEmails=you@gmail.com
# Later deploys keep the existing parameter values: ./deploy.sh my-bucket
set -euo pipefail

BUCKET="${1:?Usage: ./deploy.sh <deploy-bucket> [stack-name] [Param=Value ...]}"
STACK="${2:-trade-log}"
shift $(( $# >= 2 ? 2 : 1 ))

cd "$(dirname "$0")"
BUILD=.build
rm -rf "$BUILD" && mkdir -p "$BUILD/src"
cp src/app.py src/index.html "$BUILD/src/"
cp template.yaml "$BUILD/template.yaml"

aws cloudformation package --template-file "$BUILD/template.yaml" --s3-bucket "$BUCKET" \
  --s3-prefix "$STACK" --output-template-file "$BUILD/packaged.yaml" >/dev/null
aws cloudformation deploy --template-file "$BUILD/packaged.yaml" --stack-name "$STACK" \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND ${1:+--parameter-overrides "$@"}
aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='Url'].OutputValue" --output text
