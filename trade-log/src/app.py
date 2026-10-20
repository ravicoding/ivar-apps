"""
Trade Log: record stock trades (ticker, quantity, buy price/date, sell price/date) in DynamoDB
and report realized profit/loss by month.

One Lambda behind an HTTP API serves both the page (GET /) and the JSON API (/api/*), so the
page and API share an origin and need no CORS.

Routes (all /api/* need the x-app-password header when APP_PASSWORD is set):
  GET    /                       the app page
  GET    /health                 {"ok": true, "auth_required": bool}
  GET    /api/trades             every trade, newest first
  POST   /api/trades             add a trade
  PUT    /api/trades/{id}        replace a trade's fields
  DELETE /api/trades/{id}        delete a trade
  GET    /api/report?year=YYYY   monthly realized P/L for one year (default: latest year with sales)
"""

import base64
import datetime as dt
import hmac
import json
import os
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "trade-log"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
HERE = os.path.dirname(os.path.abspath(__file__))

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_NOTES = 500
CENT = Decimal("0.01")


# ------------------------------------------------------------------ helpers

def now_iso():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def money(d):
    """Decimal -> float rounded to cents, for JSON."""
    return float(Decimal(d).quantize(CENT, rounding=ROUND_HALF_UP))


def plain(obj):
    """DynamoDB Decimals -> JSON-friendly numbers."""
    if isinstance(obj, list):
        return [plain(v) for v in obj]
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def respond(status, body, content_type="application/json"):
    headers = {"Content-Type": content_type, "Cache-Control": "no-store",
               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}
    if content_type.startswith("text/html"):
        headers["X-Frame-Options"] = "DENY"
    return {"statusCode": status, "headers": headers,
            "body": json.dumps(body) if content_type == "application/json" else body}


def error(status, message):
    return respond(status, {"error": message})


def authorized(headers):
    if not APP_PASSWORD:
        return True
    return hmac.compare_digest(headers.get("x-app-password", "").encode(), APP_PASSWORD.encode())


def read_json(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


# ------------------------------------------------------------------ validation

class Invalid(ValueError):
    pass


def number(value, label, required=True):
    if value is None or str(value).strip() == "":
        if required:
            raise Invalid(f"Enter the {label}.")
        return None
    try:
        n = Decimal(str(value).strip().replace(",", "").lstrip("$"))
    except InvalidOperation:
        raise Invalid(f"The {label} must be a number, like 12.50.")
    if not n.is_finite() or n <= 0:
        raise Invalid(f"The {label} must be above zero.")
    if n > Decimal("1e9"):
        raise Invalid(f"The {label} is too large.")
    return n.quantize(Decimal("0.000001")).normalize()


def date(value, label, required=True):
    if value is None or str(value).strip() == "":
        if required:
            raise Invalid(f"Enter the {label}.")
        return None
    try:
        d = dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise Invalid(f"The {label} must be a date like 2026-10-20.")
    if d > dt.date.today() + dt.timedelta(days=1):  # a day of slack for time zones
        raise Invalid(f"The {label} can't be in the future.")
    if d.year < 1970:
        raise Invalid(f"The {label} is too far in the past.")
    return d.isoformat()


def trade_fields(body):
    """Validate a trade from the page. Returns the stored fields (Decimals and ISO dates)."""
    ticker = str(body.get("ticker", "")).strip().upper()
    if not TICKER_RE.match(ticker):
        raise Invalid("Enter a ticker symbol, like AAPL or BRK.B.")
    fields = {
        "ticker": ticker,
        "quantity": number(body.get("quantity"), "quantity"),
        "buy_price": number(body.get("buy_price"), "buy price"),
        "buy_date": date(body.get("buy_date"), "buy date"),
        "sell_price": number(body.get("sell_price"), "sell price", required=False),
        "sell_date": date(body.get("sell_date"), "sell date", required=False),
    }
    if (fields["sell_price"] is None) != (fields["sell_date"] is None):
        raise Invalid("Enter both the sell price and the sell date, or leave both blank for a position you still hold.")
    if fields["sell_date"] and fields["sell_date"] < fields["buy_date"]:
        raise Invalid("The sell date can't be before the buy date.")
    notes = str(body.get("notes") or "").strip()
    if len(notes) > MAX_NOTES:
        raise Invalid(f"Notes can be at most {MAX_NOTES} characters.")
    fields["notes"] = notes or None
    return fields


def computed(item):
    """Add cost, proceeds and profit/loss to a stored trade, for the page."""
    t = plain(item)
    qty, buy = Decimal(str(item["quantity"])), Decimal(str(item["buy_price"]))
    t["cost"] = money(qty * buy)
    if item.get("sell_price") is not None:
        proceeds = qty * Decimal(str(item["sell_price"]))
        t["proceeds"] = money(proceeds)
        t["pnl"] = money(proceeds - qty * buy)
        t["pnl_pct"] = float((proceeds / (qty * buy) - 1) * 100) if buy else 0.0
        t["status"] = "closed"
    else:
        t["status"] = "open"
    return t


# ------------------------------------------------------------------ data

def all_trades():
    items, kwargs = [], {}
    while True:
        page = TABLE.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def sort_key(t):
    return (t.get("sell_date") or t["buy_date"], t.get("created_at", ""))


def list_trades():
    return sorted((computed(i) for i in all_trades()), key=sort_key, reverse=True)


def save(item):
    TABLE.put_item(Item={k: v for k, v in item.items() if v is not None})


# ------------------------------------------------------------------ report

def monthly_report(trades, year=None):
    """Realized P/L by the month each trade was sold. Open positions are listed separately."""
    closed = [t for t in trades if t.get("sell_date")]
    years = sorted({t["sell_date"][:4] for t in closed}, reverse=True)
    if year is None:
        year = years[0] if years else str(dt.date.today().year)
    months = []
    for m in range(1, 13):
        key = f"{year}-{m:02d}"
        rows = [t for t in closed if t["sell_date"][:7] == key]
        cost = sum((Decimal(str(t["quantity"])) * Decimal(str(t["buy_price"])) for t in rows), Decimal(0))
        proceeds = sum((Decimal(str(t["quantity"])) * Decimal(str(t["sell_price"])) for t in rows), Decimal(0))
        pnls = [Decimal(str(t["quantity"])) * (Decimal(str(t["sell_price"])) - Decimal(str(t["buy_price"]))) for t in rows]
        months.append({
            "month": key,
            "trades": len(rows),
            "wins": sum(1 for p in pnls if p > 0),
            "losses": sum(1 for p in pnls if p < 0),
            "cost": money(cost),
            "proceeds": money(proceeds),
            "pnl": money(proceeds - cost),
            "pnl_pct": float((proceeds / cost - 1) * 100) if cost else None,
            "trade_ids": [t["id"] for t in sorted(rows, key=sort_key)],
        })
    total_cost = sum((Decimal(str(m["cost"])) for m in months), Decimal(0))
    total_pnl = sum((Decimal(str(m["pnl"])) for m in months), Decimal(0))
    open_rows = [t for t in trades if not t.get("sell_date")]
    return {
        "year": year,
        "years": years if year in years else sorted(set(years) | {year}, reverse=True),
        "months": months,
        "total": {
            "trades": sum(m["trades"] for m in months),
            "wins": sum(m["wins"] for m in months),
            "losses": sum(m["losses"] for m in months),
            "cost": money(total_cost),
            "pnl": money(total_pnl),
            "pnl_pct": float(total_pnl / total_cost * 100) if total_cost else None,
        },
        "open": {
            "positions": len(open_rows),
            "cost": money(sum((Decimal(str(t["quantity"])) * Decimal(str(t["buy_price"])) for t in open_rows), Decimal(0))),
        },
    }


# ------------------------------------------------------------------ HTTP

def page():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        return respond(200, f.read(), "text/html; charset=utf-8")


def create_trade(event):
    body = read_json(event)
    if body is None:
        return error(400, "Send the trade as a JSON object.")
    try:
        fields = trade_fields(body)
    except Invalid as e:
        return error(400, str(e))
    stamp = now_iso()
    item = {"id": uuid.uuid4().hex[:12], **fields, "created_at": stamp, "updated_at": stamp}
    save(item)
    return respond(201, computed(item))


def update_trade(trade_id, event):
    body = read_json(event)
    if body is None:
        return error(400, "Send the trade as a JSON object.")
    old = TABLE.get_item(Key={"id": trade_id}).get("Item")
    if not old:
        return error(404, "That trade no longer exists. Refresh the page.")
    try:
        fields = trade_fields(body)
    except Invalid as e:
        return error(400, str(e))
    item = {"id": trade_id, **fields, "created_at": old.get("created_at", now_iso()), "updated_at": now_iso()}
    save(item)
    return respond(200, computed(item))


def handle(event):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if method == "GET" and path in ("/", "/index.html"):
        return page()
    if method == "GET" and path == "/health":
        return respond(200, {"ok": True, "auth_required": bool(APP_PASSWORD)})
    if not path.startswith("/api/"):
        return error(404, "Not found.")
    if not authorized(headers):
        return error(401, "Wrong password.")

    if path == "/api/trades":
        if method == "GET":
            return respond(200, {"trades": list_trades()})
        if method == "POST":
            return create_trade(event)
    if path.startswith("/api/trades/"):
        trade_id = path.rsplit("/", 1)[-1]
        if not ID_RE.match(trade_id):
            return error(404, "That trade no longer exists. Refresh the page.")
        if method == "PUT":
            return update_trade(trade_id, event)
        if method == "DELETE":
            TABLE.delete_item(Key={"id": trade_id})
            return respond(200, {"ok": True})
    if method == "GET" and path == "/api/report":
        year = ((event.get("queryStringParameters") or {}).get("year") or "").strip() or None
        if year is not None and not re.fullmatch(r"(19|20)\d\d", year):
            return error(400, "Year must look like 2026.")
        return respond(200, monthly_report([computed(i) for i in all_trades()], year))
    return error(404, "Not found.")


def handler(event, context):
    try:
        return handle(event)
    except Exception as e:  # never leak a stack trace to the page
        print(json.dumps({"message": "unhandled_error", "error": repr(e)}))
        return error(500, "Something went wrong. Try again.")
