# Ivar Solutions apps

| Folder | What it is |
|---|---|
| `web/ivar-solutions/` | Company homepage (`index.html`) |
| `web/daylog/` | Daylog (Beta): daily activity log with photos, offline-first, syncs to the API (`daylog.html`) |
| `web/daybook/` | Daybook: nightly finance log (single file, stores data in the browser) |
| `daylog-api/` | AWS SAM backend for Daylog: Lambda, API Gateway, DynamoDB, S3, Cognito. See its README |
| `stock-alerts/` | Price Alerts: low/high price ranges per stock, checked every 5 minutes in market hours, text (SNS) and ntfy push alerts (AWS SAM: Lambda + Function URL, DynamoDB, EventBridge Scheduler). Page at ivarsolutions.net/alerts/. See its README |
| `market-pulse/` | Market Pulse: stock news and US economic calendar (AWS SAM: scheduled Lambda, DynamoDB, HTTP API). Live at marketpulse.ivarsolutions.net. See its README |

## Deploy the web pages

```bash
aws s3 cp web/daylog/daylog.html s3://YOUR-SITE-BUCKET/daylog.html --cache-control "max-age=300" --content-type "text/html"
aws s3 cp web/ivar-solutions/index.html s3://YOUR-SITE-BUCKET/index.html --cache-control "max-age=300" --content-type "text/html"
aws cloudfront create-invalidation --distribution-id YOUR_DIST_ID --paths "/*"
```

`web/daylog/daylog.html` has the API address and app client ID in its `CONFIG` block. They identify the API and sign-in app; they are not secrets. Never commit passwords or AWS keys.
