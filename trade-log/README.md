# Trade Log

Stock and option trade log at **https://log.ivarsolutions.net**: record each trade (ticker, quantity,
opening price and date, closing price and date, notes; for options also call/put, long/short, strike
and expiration) in DynamoDB and see realized profit/loss by month.

- `src/app.py` – one Lambda that serves the page (`GET /`) and the JSON API (`/api/*`)
- `src/index.html` – the page (ivarsolutions.net masthead, Trades and Monthly report tabs)
- `template.yaml` – DynamoDB table (point-in-time backups, kept if the stack is deleted), Lambda,
  HTTP API, ACM certificate and the `log.ivarsolutions.net` domain + Route 53 record
- Sign-in: Google, through the Daylog Cognito user pool (the stack adds its own app client). API
  Gateway checks the ID token on every `/api/*` call, and the Lambda allows only `AllowedEmails`.
- `tests/` – `python -m unittest discover -s tests` (needs boto3 installed)

## Deploy

```
./deploy.sh <deploy-bucket> trade-log AllowedEmails=you@gmail.com   # first time
./deploy.sh <deploy-bucket>                                          # later: keeps the parameters
```

## How profit/loss is counted

A trade is one buy lot. Leave the sell price and date blank while you hold it; fill them in when
you sell. The monthly report counts each sold trade in the month of its **sell date**:
P/L = quantity × (sell price − buy price). Open positions are listed but not in P/L; fees and
taxes aren't tracked. Selling part of a lot: edit the lot down to the shares you kept and add a
new trade for the shares you sold.

**Options.** Quantity is contracts and prices are per share, so P/L = contracts × 100 × price change.
`buy_*` is always the opening leg and `sell_*` the closing leg: for a **short** option (sold to open)
the opening price is the premium received and P/L = contracts × 100 × (premium − buy-back price).
An option that expired worthless closes at $0 (the page offers **Mark expired** once it's past
expiration). In the report, cost for a short option is the premium received, and return = P/L ÷ cost.
Trades saved before options were added have no `asset` field and are treated as stocks.

## API

All `/api/*` routes need `Authorization: Bearer <Cognito ID token>` from an allowed Google account.

| Method | Path | |
|---|---|---|
| GET | `/api/me` | the signed-in email |
| GET | `/api/trades` | all trades, newest first, with cost/proceeds/P&L |
| POST | `/api/trades` | add a trade |
| PUT | `/api/trades/{id}` | change a trade |
| DELETE | `/api/trades/{id}` | delete a trade |
| GET | `/api/report?year=2026` | monthly realized P/L for a year |
