# Deploying Market Pulse to marketpulse.ivarsolutions.net

Total time: about 20–30 minutes, most of it waiting for the certificate and DNS.

## 0. What you need

| Item | How to get it |
|---|---|
| AWS CLI v2, configured | `aws --version` shows 2.x; `aws sts get-caller-identity` prints your account |
| AWS permissions | Admin is simplest. Otherwise: CloudFormation, S3, Lambda, IAM (create roles), DynamoDB, API Gateway, EventBridge Scheduler, CloudWatch Logs, ACM, Route 53 |
| A bash terminal | macOS / Linux terminal. On Windows use WSL (Ubuntu) |
| Finnhub API key (free) | https://finnhub.io/register |
| FRED API key (free) | https://fredaccount.stlouisfed.org → sign up → API Keys → Request API Key |
| Access to DNS for ivarsolutions.net | Route 53 in this AWS account (automatic), or your DNS provider's dashboard |

## 1. Unzip the project
```bash
unzip stock-news-app.zip
cd stock-news-app
chmod +x *.sh
```

## 2. Choose a region and set your keys
```bash
export AWS_REGION=us-east-1            # us-east-1 / us-east-2 / us-west-2 support the AI digest
export FINNHUB_API_KEY=paste_finnhub_key
export FRED_API_KEY=paste_fred_key
export SYMBOLS=AAPL,MSFT,NVDA,AMZN,GOOGL   # your watchlist, comma-separated
```
These `export` lines last only for the current terminal window. Re-run them in a new window
before running `deploy.sh` again.

## 3. Deploy the app
```bash
./deploy.sh
```
Takes 2–3 minutes. At the end it prints:
```
Web app:    https://abc123xyz.execute-api.us-east-1.amazonaws.com/
Access key: Xy7...   (also saved in .access_key)
```
Open that Web app URL, enter the access key, and check that news and the economic calendar appear.
(On weekends or before 7am ET there may be little news; the calendar should still show.)

## 4. Connect marketpulse.ivarsolutions.net
```bash
./setup-domain.sh marketpulse.ivarsolutions.net
```

### If ivarsolutions.net is in Route 53 (same AWS account)
Nothing else to do. The script creates the certificate, validates it, and adds the DNS record.
Wait for `Done.`

### If DNS is somewhere else (GoDaddy, Cloudflare, Namecheap, Google/Squarespace, etc.)
The script stops twice and shows a record to add. In your DNS provider's dashboard for
**ivarsolutions.net**, add each one as shown:

**Record 1: certificate validation** (script waits for you to press Enter)
| Type | Name / Host | Value / Points to |
|---|---|---|
| CNAME | `_abc123….marketpulse` (copy from script) | `_def456….acm-validations.aws.` (copy from script) |

Most providers add `.ivarsolutions.net` automatically, so enter only the part before it in the
Name field. Then press Enter in the terminal; it waits until AWS issues the certificate
(usually 1–10 minutes, sometimes up to 30).

**Record 2: the site itself** (printed at the end)
| Type | Name / Host | Value / Points to |
|---|---|---|
| CNAME | `marketpulse` | `d-xxxxxxxx.execute-api.us-east-1.amazonaws.com` |

Cloudflare users: set both records to **DNS only** (grey cloud), not Proxied.

## 5. Open the app
Go to **https://marketpulse.ivarsolutions.net** and enter the access key (shown again at the end
of `setup-domain.sh`, and stored in `.access_key`). DNS can take 5–30 minutes to update.

## 6. Optional: AI market digest
1. AWS console → Amazon Bedrock → Model access → enable the Anthropic Claude models
   (first time asks for a short use-case form).
2. Redeploy with the model set:
```bash
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 ./deploy.sh
```
Digests appear at 8:00, 12:00 and 17:00 ET. Adds about $1/month.

## 7. Recommended: spending alert
Emails you if the month's AWS bill passes $4 (expected cost is about $0.10–1.10/month):
```bash
aws budgets create-budget \
  --account-id $(aws sts get-caller-identity --query Account --output text) \
  --budget '{"BudgetName":"market-pulse","BudgetLimit":{"Amount":"5","Unit":"USD"},"TimeUnit":"MONTHLY","BudgetType":"COST"}' \
  --notifications-with-subscribers '[{"Notification":{"NotificationType":"ACTUAL","ComparisonOperator":"GREATER_THAN","Threshold":80,"ThresholdType":"PERCENTAGE"},"Subscribers":[{"SubscriptionType":"EMAIL","Address":"you@example.com"}]}]'
```

## Day-to-day
| Task | Command |
|---|---|
| Update the app or change settings | set the `export` lines again, then `./deploy.sh` (domain and access key stay) |
| Change watchlist | `SYMBOLS=AAPL,TSLA,META ./deploy.sh` |
| See today's data in the terminal | `./today.sh` |
| Collect now (don't wait 15 min) | `aws lambda invoke --function-name $(aws cloudformation describe-stacks --stack-name stock-news --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text) /dev/stdout` |
| Watch collector logs | `aws logs tail /aws/lambda/$(aws cloudformation describe-stacks --stack-name stock-news --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text) --follow` |
| Change the access key | `rm .access_key && ./deploy.sh` (prints the new key) |

## Troubleshooting
| Symptom | Fix |
|---|---|
| `deploy.sh`: "Set FINNHUB_API_KEY first" | Run the `export` lines from step 2 in this terminal |
| Page keeps asking for the access key | Use the value in `.access_key` exactly (no spaces) |
| Economic calendar empty | `FRED_API_KEY` wasn't set when deploying; export it and run `./deploy.sh` again |
| No news | Normal on weekends/overnight. Run the "Collect now" command and check the logs |
| Certificate stuck at "pending" | Check record 1 is exact (no doubled `.ivarsolutions.net`), Cloudflare proxy off; check with `nslookup -type=CNAME <record name>` |
| Domain shows `{"message":"Not Found"}` | The mapping is missing; re-run `./setup-domain.sh marketpulse.ivarsolutions.net` |
| Domain doesn't resolve | Record 2 missing or DNS still updating; check with `nslookup marketpulse.ivarsolutions.net` |
| Browser warns about the certificate | You're on the old execute-api URL with the custom name, or DNS points elsewhere; recheck record 2 |

## Removing everything
Run in this order (the domain must be removed before the stack):
```bash
aws apigatewayv2 delete-domain-name --domain-name marketpulse.ivarsolutions.net
aws cloudformation delete-stack --stack-name stock-news
aws cloudformation wait stack-delete-complete --stack-name stock-news
aws acm delete-certificate --certificate-arn $(aws acm list-certificates --query "CertificateSummaryList[?DomainName=='marketpulse.ivarsolutions.net'].CertificateArn | [0]" --output text)
aws s3 rb s3://stock-news-artifacts-$(aws sts get-caller-identity --query Account --output text)-$AWS_REGION --force
```
Then delete the two DNS records for marketpulse at your DNS provider (or in Route 53).
The DynamoDB table is deleted with the stack, along with all stored news.
