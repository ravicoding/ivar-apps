"""
Trade Log: record stock and option trades in DynamoDB and report realized profit/loss by month.

A trade has an opening leg (buy_price/buy_date) and an optional closing leg (sell_price/sell_date).
For stocks and long options the opening leg is a purchase. For a short option (sold to open) the
opening leg is the sale and the closing leg is the buy-back. Options also store option_type
(call/put), side (long/short), strike and expiration; prices are per share and one contract is
100 shares. Trades saved before options existed have no asset field and are stocks.

One Lambda behind an HTTP API serves both the page (GET /) and the JSON API (/api/*), so the
page and API share an origin and need no CORS.

Sign-in is Google through a Cognito user pool. API Gateway checks the ID token on every /api/*
route (JWT authorizer); this code then allows only the emails in ALLOWED_EMAILS.

Routes:
  GET    /                       the app page (public; it shows the Google sign-in)
  GET    /health                 {"ok": true}
  GET    /api/me                 the signed-in email
  GET    /api/trades             every trade, newest first
  POST   /api/trades             add a trade
  PUT    /api/trades/{id}        replace a trade's fields
  DELETE /api/trades/{id}        delete a trade
  GET    /api/report?year=YYYY   monthly realized P/L for one year (default: latest year with sales)
"""

import base64
import datetime as dt
import json
import os
import re
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import boto3

TABLE = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "trade-log"))
ALLOWED_EMAILS = {e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()}
CLIENT_ID = os.environ.get("CLIENT_ID", "")
AUTH_DOMAIN = os.environ.get("AUTH_DOMAIN", "")
HERE = os.path.dirname(os.path.abspath(__file__))

TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,14}$")
ID_RE = re.compile(r"^[0-9a-f]{12}$")
MAX_NOTES = 500
CENT = Decimal("0.01")
MULTIPLIER = {"stock": Decimal(1), "option": Decimal(100)}  # shares per unit of quantity
OPTION_TYPES = ("call", "put")
SIDES = ("long", "short")


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


def signed_in_email(event):
    """Email from the ID token API Gateway already verified, or None. Fails closed if no authorizer ran."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("jwt", {}).get("claims") or {}
    email = str(claims.get("email", "")).lower()
    if not email or claims.get("token_use") != "id" or str(claims.get("email_verified")).lower() != "true":
        return None
    return email


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


def number(value, label, required=True, allow_zero=False):
    if value is None or str(value).strip() == "":
        if required:
            raise Invalid(f"Enter the {label}.")
        return None
    try:
        n = Decimal(str(value).strip().replace(",", "").lstrip("$"))
    except InvalidOperation:
        raise Invalid(f"The {label} must be a number, like 12.50.")
    if not n.is_finite() or n < 0 or (n == 0 and not allow_zero):
        raise Invalid(f"The {label} can't be negative." if allow_zero else f"The {label} must be above zero.")
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


def expiration_date(value):
    """An option's expiration: unlike trade dates it may be in the future."""
    if value is None or str(value).strip() == "":
        raise Invalid("Enter the expiration date.")
    try:
        d = dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise Invalid("The expiration must be a date like 2026-12-18.")
    if not 1970 <= d.year <= 2100:
        raise Invalid("The expiration date isn't valid.")
    return d.isoformat()


def choice(value, allowed, default, message):
    v = str(value or default).strip().lower()
    if v not in allowed:
        raise Invalid(message)
    return v


def trade_fields(body):
    """Validate a trade from the page. Returns the stored fields (Decimals and ISO dates)."""
    asset = choice(body.get("asset"), MULTIPLIER, "stock", "The type must be stock or option.")
    opt = asset == "option"
    ticker = str(body.get("ticker", "")).strip().upper()
    if not TICKER_RE.match(ticker):
        raise Invalid("Enter a ticker symbol, like AAPL or BRK.B.")
    open_word, close_word = ("opening", "closing") if opt else ("buy", "sell")
    fields = {
        "asset": asset,
        "ticker": ticker,
        "quantity": number(body.get("quantity"), "number of contracts" if opt else "quantity"),
        "buy_price": number(body.get("buy_price"), f"{open_word} price"),
        "buy_date": date(body.get("buy_date"), f"{open_word} date"),
        # An option that expires worthless closes at $0.
        "sell_price": number(body.get("sell_price"), f"{close_word} price", required=False, allow_zero=opt),
        "sell_date": date(body.get("sell_date"), f"{close_word} date", required=False),
        "option_type": None, "side": None, "strike": None, "expiration": None,
    }
    if (fields["sell_price"] is None) != (fields["sell_date"] is None):
        if opt:
            raise Invalid("Enter both the closing price and the closing date, or leave both blank for a position that's still open.")
        raise Invalid("Enter both the sell price and the sell date, or leave both blank for a position you still hold.")
    if fields["sell_date"] and fields["sell_date"] < fields["buy_date"]:
        raise Invalid(f"The {close_word} date can't be before the {open_word} date.")
    if opt:
        fields["option_type"] = choice(body.get("option_type"), OPTION_TYPES, "", "Choose call or put.")
        fields["side"] = choice(body.get("side"), SIDES, "long", "The position must be long or short.")
        fields["strike"] = number(body.get("strike"), "strike price")
        fields["expiration"] = expiration_date(body.get("expiration"))
        if fields["buy_date"] > fields["expiration"]:
            raise Invalid("The opening date can't be after the expiration date.")
        if fields["sell_date"] and fields["sell_date"] > fields["expiration"]:
            raise Invalid("The closing date can't be after the expiration date.")
    notes = str(body.get("notes") or "").strip()
    if len(notes) > MAX_NOTES:
        raise Invalid(f"Notes can be at most {MAX_NOTES} characters.")
    fields["notes"] = notes or None
    return fields


def amounts(t):
    """(cost, proceeds, pnl) as Decimals; proceeds and pnl are None while the trade is open.

    cost is the opening leg's value: what was paid, or for a short option the premium received.
    proceeds = cost + pnl, so the report's columns always add up and return = pnl / cost.
    """
    shares = Decimal(str(t["quantity"])) * MULTIPLIER[t.get("asset") or "stock"]
    cost = shares * Decimal(str(t["buy_price"]))
    if t.get("sell_price") is None:
        return cost, None, None
    closing = shares * Decimal(str(t["sell_price"]))
    pnl = cost - closing if t.get("side") == "short" else closing - cost
    return cost, cost + pnl, pnl


def computed(item):
    """Add cost, proceeds and profit/loss to a stored trade, for the page."""
    t = plain(item)
    t.setdefault("asset", "stock")
    cost, proceeds, pnl = amounts(item)
    t["cost"] = money(cost)
    if pnl is not None:
        t["proceeds"] = money(proceeds)
        t["pnl"] = money(pnl)
        t["pnl_pct"] = float(pnl / cost * 100) if cost else 0.0
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
        sums = [amounts(t) for t in rows]
        cost = sum((a[0] for a in sums), Decimal(0))
        proceeds = sum((a[1] for a in sums), Decimal(0))
        pnls = [a[2] for a in sums]
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
            # Money paid into positions still held; premium from open short options isn't a cost.
            "cost": money(sum((amounts(t)[0] for t in open_rows if t.get("side") != "short"), Decimal(0))),
        },
    }


# ------------------------------------------------------------------ HTTP

def page():
    with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
        html = f.read()
    config = json.dumps({"clientId": CLIENT_ID, "authDomain": AUTH_DOMAIN}).replace("<", "\\u003c")
    return respond(200, html.replace("/*SIGN_IN_CONFIG*/{}", config), "text/html; charset=utf-8")


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

    if method == "GET" and path in ("/", "/index.html"):
        return page()
    if method == "GET" and path == "/health":
        return respond(200, {"ok": True})
    if not path.startswith("/api/"):
        return error(404, "Not found.")
    email = signed_in_email(event)
    if not email:
        return error(401, "Sign in with Google.")
    if email not in ALLOWED_EMAILS:
        return error(403, f"{email} isn't allowed to use Trade Log.")
    if method == "GET" and path == "/api/me":
        return respond(200, {"email": email})

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
