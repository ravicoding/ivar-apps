# Ivar Solutions apps

| Folder | What it is |
|---|---|
| `web/ivar-solutions/` | Company homepage (`index.html`), plus the publications pages: `publish.html` (editor, drafts kept in the browser), `publication.html` (one published item) and `pub-render.js` (shared formatter) |
| `web/daylog/` | Daylog (Beta): daily activity log with photos, offline-first, syncs to the API (`daylog.html`) |
| `web/daybook/` | Daybook: nightly finance log (single file, stores data in the browser) |
| `daylog-api/` | AWS SAM backend for Daylog: Lambda, API Gateway, DynamoDB, S3, Cognito. See its README |
| `publications-api/` | AWS SAM backend for `publish.html`: Lambda and API Gateway that write publications to the site bucket and refresh CloudFront. Sign-in reuses the Daylog Cognito pool. See its README |
| `market-pulse/` | Market Pulse: stock news and US economic calendar (AWS SAM: scheduled Lambda, DynamoDB, HTTP API). Live at marketpulse.ivarsolutions.net. See its README |

## Deploy the web pages

```bash
aws s3 cp web/daylog/daylog.html s3://YOUR-SITE-BUCKET/daylog.html --cache-control "max-age=300" --content-type "text/html"
for f in index.html publish.html publication.html; do
  aws s3 cp web/ivar-solutions/$f s3://YOUR-SITE-BUCKET/$f --cache-control "max-age=300" --content-type "text/html"
done
aws s3 cp web/ivar-solutions/pub-render.js s3://YOUR-SITE-BUCKET/pub-render.js --cache-control "max-age=300" --content-type "application/javascript"
aws cloudfront create-invalidation --distribution-id YOUR_DIST_ID --paths "/*"
```

`web/daylog/daylog.html` has the API address and app client ID in its `CONFIG` block. They identify the API and sign-in app; they are not secrets. Never commit passwords or AWS keys.
