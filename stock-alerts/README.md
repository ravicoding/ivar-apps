# Price Alerts

Enter a stock ticker and a low/high price range on a page hosted in your S3 bucket. A Lambda
checks prices every 5 minutes, 9:30 AM to 4:00 PM Eastern, Monday to Friday, and
texts your phone when a target is hit.

```
ivarsolutions.net/alerts/  (S3: index.html + config.js)
        │  HTTPS + password header
        ▼
Lambda Function URL (API) ──> DynamoDB (alerts, settings)
        ▲
EventBridge Scheduler, every 5 min in market hours (ET)
        └─> Lambda checks Yahoo prices ──> Amazon SNS text ──> your phone
                                       └─> ntfy app push (optional backup)
```

## What's in here

| File | What it is |
|---|---|
| `template.yaml` | AWS SAM template: Lambda + Function URL (with CORS), DynamoDB table, 3 schedules |
| `src/app.py` | The Lambda: scheduled price checks, push notifications, and the API |
| `tests/test_ranges.py` | Tests for range alerts: `python3 -m unittest discover -s tests` (needs boto3) |
| `web/index.html` | The settings page (static, goes in S3) |
| `web/config.js` | Tells the page where the API is; filled in by `deploy-web.sh` |
| `deploy-web.sh` | Uploads the page to your bucket with the right API URL |

The Lambda has no packages to install; it uses Python's standard library and the
boto3 that Lambda already includes.

## 1. Deploy the Lambda

You need the [AWS CLI](https://aws.amazon.com/cli/) configured and the
[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).

```bash
cd stock-alerts
sam build
sam deploy --guided
```

When `--guided` asks:
- **Stack name:** `price-alerts`
- **AWS Region:** any (e.g. `us-east-1`)
- **Parameter AppPassword:** a password for the page (8+ characters)
- **Parameter PhoneNumber:** your phone in +1XXXXXXXXXX form (blank turns texts off)
- **Parameter SmsOriginationNumber:** press Enter to leave blank for now (see step 3)
- **Parameter AllowedOrigins:** press Enter to keep the default
  (`ivarsolutions.net` and `www.`, both http and https). If you'll open the page
  from another address, such as the raw S3 website endpoint, add it here.
- **AlertsFunction Function Url has no authentication. Is this okay?** `y`
  (every API call needs your AppPassword)
- Accept the defaults for the rest.

## 2. Upload the page to S3

```bash
./deploy-web.sh                     # bucket ivarsolutions.net, folder alerts/
./deploy-web.sh ivarsolutions.net ""          # or put it at the site root
./deploy-web.sh my-bucket stocks price-alerts # bucket, folder, stack name
```

This writes the Lambda's URL into `config.js` and uploads both files. Then open
`https://ivarsolutions.net/alerts/` and enter your password.

Your bucket already serves the site, so no bucket changes are needed. If the site
is behind CloudFront, invalidate `/alerts/*` after uploading.

## 3. Turn on text messages (one-time AWS setup)

US carriers block texts that don't come from a registered number, so AWS needs a
toll-free number tied to this use before your phone will get alerts. Do this in the
same Region you deployed to, in the AWS console under **AWS End User Messaging SMS**:

1. **Request a toll-free number.** Phone numbers → Request originator → United
   States → Toll-free → capability SMS → message type **Transactional**.
2. **Register it.** Fill in the toll-free registration form. Describe the use as
   "Personal stock price alerts sent to the account owner's own phone (opt-in, one
   recipient)". Since September 15, 2026, new registrations also need a public
   Privacy Policy URL and Terms URL; you can host two short pages on
   ivarsolutions.net. AWS says approval takes up to 15 business days.
3. **Verify your phone while in the SMS sandbox.** New accounts can only text
   verified numbers: in Amazon SNS → Text messaging (SMS) → Sandbox destination
   phone numbers, add your phone number and enter the code you receive. (Or request
   production access to skip this.)
4. **Point the Lambda at the approved number:**
   ```bash
   sam deploy --parameter-overrides AppPassword=yourpassword SmsOriginationNumber=+18885551234
   ```
5. On the page, click **Send test** under Phone notifications.

Texts cost roughly $2 a month for the toll-free number plus under a cent per
message (check current AWS End User Messaging SMS pricing).

### Get alerts now with app push (optional, recommended while you wait)

1. Install **ntfy** on your phone (free, App Store / Google Play, no account).
2. On the page, open **App push**, click **Generate**, then **Save**.
3. In the ntfy app, tap **+** and subscribe to that exact topic name.
4. Click **Send test**. You'll get both a text (once SMS is set up) and a push.

## Using it

- Add a ticker with a **low** and a **high** price. You get a text (and an ntfy push,
  if set up) when the price drops to or below the low, or rises to or above the high.
- Leave one side blank to watch only the other (for example, only a low).
- Each range alerts **once per crossing**: after it fires, it stays quiet until the
  price comes back inside the range, then it can fire again. The watchlist shows
  whether each stock is in range, below or above, and how many alerts were sent.
- If the price is already outside the range when you add it, the next check alerts.
- Older single-target alerts (from before ranges) still work: they fire once and
  move to the bottom marked "Hit".
- **Check now** runs a check immediately, even outside market hours.

## Cost

Lambda, DynamoDB and S3 stay within the free tier for personal use (about 1,700
runs a month). Texts are the only real cost; see step 3.

## Privacy

Your phone number is stored only as a Lambda setting. The public page never
contains it; it shows just the last four digits, and only after you unlock it.

## Notes

- **Password:** it's stored in the browser you unlock from, and sent with each
  API call over HTTPS. Use the https:// address of your site if you have one.
- **Test says "Sent by text" but nothing arrived:** SNS accepted the message but
  a carrier dropped it. That almost always means the toll-free number isn't
  approved yet or SmsOriginationNumber isn't set. Check SNS → Text messaging →
  delivery status logs.
- **Holidays:** checks still run on market holidays; prices don't move, so nothing fires.
- **Price source:** Yahoo Finance's public chart endpoint (no key needed). It's
  unofficial and can occasionally throttle cloud IPs; errors show when you hover
  the status line. To use a keyed API (Finnhub, Alpha Vantage, Polygon), change
  only `get_price()` in `src/app.py`.
- **"Couldn't reach the alerts service":** usually the page's address isn't in
  AllowedOrigins. Redeploy with
  `sam deploy --parameter-overrides AllowedOrigins="https://ivarsolutions.net,https://your-other-address"`
  (include AppPassword again too).
- **Changing the schedule:** edit the three `cron(...)` lines in `template.yaml`,
  then `sam build && sam deploy`.
- **Logs:** `sam logs -n AlertsFunction --stack-name price-alerts --tail`
- **Remove everything:** `sam delete --stack-name price-alerts`, then delete
  `alerts/` from the bucket.
- **403 Forbidden from the API URL:** SAM normally adds the URL permissions. If not:
  ```bash
  FN=$(aws cloudformation describe-stack-resource --stack-name price-alerts \
       --logical-resource-id AlertsFunction --query StackResourceDetail.PhysicalResourceId --output text)
  aws lambda add-permission --function-name $FN --statement-id UrlInvoke \
       --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE
  aws lambda add-permission --function-name $FN --statement-id UrlInvokeFn \
       --action lambda:InvokeFunction --principal '*' --invoked-via-function-url
  ```
