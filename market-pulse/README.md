# Market Pulse: stock news & economic calendar on AWS

A small serverless app that, every 15 minutes on weekdays (7:00–18:45 US Eastern), collects:

- **Market news** and news for your **watchlist** (Finnhub)
- Today's **earnings** and **IPOs** (Finnhub)
- The **US economic calendar** with the latest numbers (FRED, St. Louis Fed):
  CPI, nonfarm payrolls & unemployment rate, PCE, GDP, PPI, retail sales,
  JOLTS job openings, weekly jobless claims, and **FOMC** rate decisions
- Optional **AI digest** 3× a day (Claude Haiku on Amazon Bedrock)

Everything is stored as brief summaries in DynamoDB, and a web page lets you read it.

```
EventBridge Scheduler ─every 15 min─▶ Collector Lambda ──▶ DynamoDB ◀── Reader Lambda ◀── HTTP API ◀── your browser
                                       │ Finnhub (news, earnings, IPOs)                 (serves the page
                                       │ FRED (economic calendar + values)               + JSON, read-only)
                                       └ Bedrock (optional digest)
```

## What you need
- AWS CLI configured (`aws sts get-caller-identity` works)
- Free **Finnhub** key: https://finnhub.io/register
- Free **FRED** key: https://fredaccount.stlouisfed.org (sign up, then API Keys → Request API key)
- Optional, for AI digests: Anthropic models enabled in the Bedrock console

## Deploy
```bash
export FINNHUB_API_KEY=your_finnhub_key
export FRED_API_KEY=your_fred_key
export SYMBOLS=AAPL,MSFT,NVDA,AMZN,GOOGL,TSLA      # your watchlist (optional)
./deploy.sh

# with AI digests
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0 ./deploy.sh
```
At the end it prints the **web app URL** and an **access key**. Open the URL, enter the key once
(it's remembered in that browser). The key is saved in `.access_key` so redeploys keep it;
set `ACCESS_KEY=...` to choose your own.

## Use your own domain (e.g. marketpulse.ivarsolutions.net)
```bash
./setup-domain.sh marketpulse.ivarsolutions.net
```
Run it after `deploy.sh`. It requests a free TLS certificate (ACM), creates an API Gateway custom
domain, maps it to the app, and points DNS at it. If `ivarsolutions.net` is hosted in Route 53 in
the same account, DNS is fully automatic; otherwise it prints two CNAME records to add at your
DNS provider. Cost: $0 (ACM public certificates and API Gateway custom domains are free;
a Route 53 hosted zone, if you use one, is $0.50/month).

## The web app (`src/web/index.html`)
- **Day timeline**: pre-market, regular session and after-hours, with markers for scheduled
  releases (CPI 8:30, Fed 2:00 pm…), earnings before the open / after the close, and bars showing
  how much news arrived every 15 minutes. Select a bar to read just those stories.
- **AI digest** (if enabled), with a button for each digest of the day
- **News**: All / Watchlist / Market filter and search
- **Economic calendar**: the next two weeks, high-impact releases marked, latest values
  (e.g. "Headline CPI +0.3% m/m, 2.9% y/y (Sep 2026)", "Payrolls +142K jobs")
- **Earnings today** (watchlist first) and **IPOs today**
- Previous days via the date picker; auto-refreshes every 5 minutes while viewing today;
  works on phones and follows light/dark mode

Preview without deploying: open `src/web/index.html` directly in a browser (it shows sample data).

## Economic calendar details
| Release | Time (ET) | Values stored |
|---|---|---|
| CPI | 8:30 | headline & core, m/m and y/y |
| Employment Situation (NFP) | 8:30 | payroll change, unemployment rate |
| PCE (Personal Income and Outlays) | 8:30 | PCE & core PCE, m/m and y/y |
| GDP | 8:30 | real GDP, annualized q/q |
| PPI | 8:30 | final demand, m/m and y/y |
| Retail sales | 8:30 | m/m |
| JOLTS | 10:00 | job openings |
| Jobless claims (weekly) | 8:30 | initial claims |
| FOMC decision | 2:00 pm | fed funds target range |

The calendar is refreshed on the first run of the day and again at 9:00, 11:00 and 17:00, so
new numbers appear shortly after FRED publishes them (usually within an hour of the release).
FRED doesn't provide **consensus forecasts**; the page shows the latest and previous readings.
FOMC dates are built in for 2026–2027 (`src/econ.py`); add 2028 when the Fed publishes them.

## Read from the command line
```bash
./today.sh              # digests, economic releases, earnings/IPOs, news for today
./today.sh 2026-10-09   # a past day
```

## API (used by the page)
| Endpoint | Returns |
|---|---|
| `GET /api/day?date=YYYY-MM-DD` | news, events, digests, economic releases for that day |
| `GET /api/econ?from=YYYY-MM-DD&to=YYYY-MM-DD` | economic calendar between two dates |

Send the header `x-access-key: <your key>`.

## Table layout (one DynamoDB table)
| pk | sk | contents |
|---|---|---|
| `NEWS#2026-10-12` | `2026-10-12T09:41:00#<id>` | headline, summary (≤280 chars), source, url, symbols |
| `EVENT#2026-10-12` | `EARNINGS#AAPL` / `IPO#ACME` | one-line summary, estimates/actuals |
| `ECONCAL` | `2026-10-14#CPI` | release, time, importance, latest values |
| `DIGEST#2026-10-12` | `12:00` | AI digest text, token usage |

Items expire automatically (news/events after 30 days, calendar 60 days after the window).

## Monthly cost (us-east-1, default settings)
About 1,056 collector runs a month (48 per weekday × 22 days) and ~50 page views a day.

| Service | Usage | Cost |
|---|---|---|
| EventBridge Scheduler | 1,056 invocations (14M/month free) | $0.00 |
| Lambda, collector + reader (arm64) | ~1,200 GB-s (400k GB-s free) | $0.00 (≈$0.02 without free tier) |
| API Gateway HTTP API | ~1,500 requests @ $1.00/M | <$0.01 |
| DynamoDB on-demand | ~60k writes @ $0.625/M, ~75k reads @ $0.125/M, ~20 MB | ~$0.06 |
| CloudWatch Logs, S3 package | KBs, 14-day retention | <$0.01 |
| Finnhub, FRED | free tiers | $0.00 |
| **Total without AI** | | **≈ $0.10 / month** |
| Bedrock Claude Haiku 4.5 digests (optional) | 66 calls × ~13k in / 300 out tokens | ≈ $1.00 |
| **Total with AI digests** | | **≈ $1.10 / month** |

The main cost lever is how often the AI digest runs (one per 15-minute run ≈ $15/month).

## Remove everything
```bash
aws cloudformation delete-stack --stack-name stock-news
aws s3 rb s3://stock-news-artifacts-<account>-<region> --force
```

## Test locally
```bash
pip install moto boto3
python tests/test_handler.py
```

## Notes
- API keys are stored as Lambda environment variables (encrypted at rest). For stricter setups,
  move them to SSM Parameter Store or Secrets Manager.
- The web page's access key is a simple shared secret, fine for personal use. For multiple
  users, put Amazon Cognito or CloudFront + sign-in in front of the API.
- News is de-duplicated with a conditional write; earnings are overwritten at refresh times so
  reported EPS/revenue actuals fill in.
