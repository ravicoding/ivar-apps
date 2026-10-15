# Publications API

Backs `publish.html` on ivarsolutions.net. Drafts stay in the browser (localStorage). Publishing calls this API, which writes to the website bucket and invalidates CloudFront:

| File | What it is |
|---|---|
| `publications/index.json` | List shown under Publications on the homepage, newest first |
| `publications/{slug}.json` | One publication, rendered by `publication.html?p={slug}` |
| `docs/publications/{uuid}.pdf` | Optional PDF attachment (uploaded straight from the browser with a presigned form) |

Sign-in reuses the Daylog Cognito pool (`daylog-api-dev`). Only emails in `PublisherEmails` may publish; anyone else gets 403.

## Routes

| Route | What it does |
|---|---|
| `GET /health` | No sign-in. `{"ok":true}` |
| `PUT /publications/{slug}` | Publish or update. Body: `title` (required), `authors`, `summary`, `body` (Markdown subset), `tags`, `pdf`. Needs body or pdf. Keeps the original publish date on updates |
| `DELETE /publications/{slug}` | Unpublish. The PDF is kept |
| `POST /publications/upload-url` | Presigned S3 form for one PDF, 20 MB max, valid 5 minutes |

## Deploy

```bash
cd backend && npm ci --omit=dev && cd ..
aws cloudformation package --template-file template.yaml --s3-bucket daylog-deploy-ravi-160275 \
  --s3-prefix publications-api --output-template-file packaged.yaml
aws cloudformation deploy --template-file packaged.yaml --stack-name publications-api \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides UserPoolId=us-east-1_jwvM6Q2KJ UserPoolClientId=5cm9v8rbhi8u1lt5n0p5qsap3g \
    PublisherEmails=reddynr@gmail.com
```

To add a publisher, redeploy with `PublisherEmails=a@x.com,b@y.com` (each needs an account in the Daylog pool).

The website bucket has a CORS rule allowing `POST` from `https://ivarsolutions.net` so the browser can upload PDFs.

## Tests

```bash
cd backend && npm install && npm test
```
