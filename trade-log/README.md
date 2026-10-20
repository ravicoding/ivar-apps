# Trade Log

Stock trade log at **https://log.ivarsolutions.net**: record each trade (ticker, quantity, buy price
and date, sell price and date, notes) in DynamoDB and see realized profit/loss by month.

- `src/app.py` – one Lambda that serves the page (`GET /`) and the JSON API (`/api/*`)
- `src/index.html` – the page (ivarsolutions.net masthead, Trades and Monthly report tabs)
- `template.yaml` – DynamoDB table (point-in-time backups, kept if the stack is deleted), Lambda,
  HTTP API, ACM certificate and the `log.ivarsolutions.net` domain + Route 53 record
- `tests/` – `python -m unittest discover -s tests` (needs boto3 installed)

## Deploy

```
./deploy.sh <deploy-bucket> trade-log AppPassword=...   # first time
./deploy.sh <deploy-bucket>                              # later: keeps the password
```

## How profit/loss is counted

A trade is one buy lot. Leave the sell price and date blank while you hold it; fill them in when
you sell. The monthly report counts each sold trade in the month of its **sell date**:
P/L = quantity × (sell price − buy price). Open positions are listed but not in P/L; fees and
taxes aren't tracked. Selling part of a lot: edit the lot down to the shares you kept and add a
new trade for the shares you sold.

## API

All `/api/*` routes need the `x-app-password` header when a password is set.

| Method | Path | |
|---|---|---|
| GET | `/api/trades` | all trades, newest first, with cost/proceeds/P&L |
| POST | `/api/trades` | add a trade |
| PUT | `/api/trades/{id}` | change a trade |
| DELETE | `/api/trades/{id}` | delete a trade |
| GET | `/api/report?year=2026` | monthly realized P/L for a year |
