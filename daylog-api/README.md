# Daylog API

A Node.js 24 Lambda behind an API Gateway HTTP API. Entry text goes to DynamoDB, photos go to S3, and sign-in is handled by Cognito. One CloudFormation (SAM) template creates everything.

## What gets created

| Resource | Purpose |
|---|---|
| Lambda `ApiFunction` | The API in `backend/handler.mjs` |
| HTTP API | Public HTTPS endpoint, Cognito JWT required (except `/health`), throttled to 10 requests per second |
| DynamoDB table | On-demand billing, point-in-time recovery, encryption, TTL on `ttl` |
| S3 bucket | Private photo storage, uploads limited to 8 MB images |
| Cognito user pool | Email sign-in. Only an admin can create users (no public sign-up) |

The table and bucket are kept if you delete the stack, so your data is never lost by accident.

## Deploy (AWS CLI only)

You need the AWS CLI configured (`aws sts get-caller-identity` works), Node.js 22 or newer on your computer (to install the dependencies), and any S3 bucket you own for uploading the Lambda package (called `DEPLOY_BUCKET` below).

```bash
# 1. Install the production dependencies into the package
cd backend && npm ci --omit=dev && cd ..

# 2. Upload the code and produce a deployable template
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket DEPLOY_BUCKET \
  --output-template-file packaged.yaml

# 3. Create or update the stack
aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name daylog-api-dev \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides Stage=dev AllowedOrigin=https://ivarsolutions.net

# 4. Read the values you need
aws cloudformation describe-stacks --stack-name daylog-api-dev --query "Stacks[0].Outputs" --output table
```

`AllowedOrigin` is the website that will call the API (scheme and host only). Browsers block calls from any other site. For testing from a local page, redeploy with `AllowedOrigin=http://localhost:8080`.

## Create your login

Cognito will email a temporary password. Replace the placeholders, and type any password yourself in your own terminal.

```bash
aws cognito-idp admin-create-user --user-pool-id POOL_ID --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true

aws cognito-idp admin-set-user-password --user-pool-id POOL_ID --username you@example.com \
  --password 'YOUR_NEW_PASSWORD' --permanent
```

## Try it

```bash
# Get a token (use the IdToken)
aws cognito-idp initiate-auth --auth-flow USER_PASSWORD_AUTH --client-id CLIENT_ID \
  --auth-parameters USERNAME=you@example.com,PASSWORD='YOUR_NEW_PASSWORD' \
  --query AuthenticationResult.IdToken --output text

API=https://XXXX.execute-api.us-east-1.amazonaws.com
TOKEN=...paste the IdToken...

curl $API/health

# Save an entry
curl -X PUT $API/entries/$(uuidgen) -H "Authorization: $TOKEN" -H "Content-Type: application/json" \
  -d '{"date":"2026-10-10","text":"Gym for 45 minutes, then groceries"}'

# Read the day back
curl "$API/entries?date=2026-10-10" -H "Authorization: $TOKEN"
```

Tokens last one hour. Request a new one with the same command.

## API

All bodies and responses are JSON. Errors look like `{"error":{"code":"invalid_date","message":"..."}}`.

| Route | What it does |
|---|---|
| `GET /health` | No sign-in needed. Returns `{"ok":true}` |
| `PUT /entries/{id}` | Create or update an entry. Body: `date` (required), `text`, `images` (photo keys), `createdAt`, `updatedAt`. Needs text or a photo. `id` is a client-made unique id (8 to 64 letters, digits, `-`, `_`), so retries never create duplicates |
| `GET /entries?date=2026-10-10` | One day, newest first. Add `includeDeleted=true` to include deleted entries |
| `GET /entries?since=<ISO time>` | Everything created, changed or deleted after that time, oldest first. Use this to sync a device. `since=` (empty) returns everything |
| `DELETE /entries/{id}` | Marks the entry deleted. Other devices learn of it through `since`. Removed for good after 90 days |
| `POST /photos/upload-url` | Body: `{"contentType":"image/jpeg"}` (also png, webp). Returns a one-time S3 upload form valid for 5 minutes |

Both list calls take `limit` (1 to 100, default 50) and return `nextCursor`. Pass it back as `cursor` for the next page. A page can be shorter than `limit` while `nextCursor` is still set, so keep going until it is `null`.

Each listed entry includes `imageUrls`, temporary links (15 minutes) that match `images` one for one.

### Saving a photo

1. `POST /photos/upload-url` with the content type. You get `key`, `url` and `fields`.
2. Send a multipart form to `url` containing every item in `fields`, then the image file as the last field named `file`.
3. Save the entry with `"images": ["<key>"]`.

```js
const form = new FormData();
Object.entries(fields).forEach(([k, v]) => form.append(k, v));
form.append('file', blob);               // must be last
await fetch(url, { method: 'POST', body: form });
```

### Conflicts

If two devices edit the same entry, the one with the later `updatedAt` wins. When your update loses, the response is `200` with `"applied": false` and the winning version in `entry`. Adopt that version locally. A device whose clock is ahead cannot win more than 5 minutes into the future.

## Tests

```bash
cd backend && npm install && npm test
```

## Things to know

- Photos you remove from an entry, or photos of a deleted entry, stay in S3. Cleaning them up automatically needs a DynamoDB stream and a second small Lambda.
- Only the admin can create users. To allow public sign-up, set `AllowAdminCreateUserOnly` to `false` in `template.yaml`, but anyone could then store data in your account.
- Delete everything: `aws cloudformation delete-stack --stack-name daylog-api-dev`, then delete the retained table and bucket yourself.
