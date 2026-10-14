"""Read-only API + web page for the news collector (API Gateway HTTP API -> Lambda).

GET /                          the web app (web/index.html)
GET /api/day?date=YYYY-MM-DD   news, earnings/IPO events, AI digests and economic releases for a day
GET /api/econ?from=..&to=..    economic calendar between two dates (inclusive)

If ACCESS_KEY is set, /api/* requires the header  x-access-key: <ACCESS_KEY>.
"""
import hmac
import json
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("MARKET_TZ", "America/New_York"))
except Exception:  # pragma: no cover
    TZ = None

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "").split(",") if s.strip()]
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
with open(os.path.join(os.path.dirname(__file__), "web", "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read()


def _json_default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(type(o))


def respond(status, body, ctype="application/json", cache="no-store"):
    return {
        "statusCode": status,
        "headers": {"content-type": ctype, "cache-control": cache,
                    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer"},
        "body": body if isinstance(body, str) else json.dumps(body, default=_json_default),
    }


def query(cond, limit=None, newest_first=False):
    out, kw = [], {"KeyConditionExpression": cond, "ScanIndexForward": not newest_first}
    while True:
        r = table.query(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r or (limit and len(out) >= limit):
            return out[:limit] if limit else out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def strip(items, drop=("pk", "ttl")):
    return [{k: v for k, v in it.items() if k not in drop} for it in items]


def day_payload(day):
    news = query(Key("pk").eq(f"NEWS#{day}"), limit=400, newest_first=True)
    events = query(Key("pk").eq(f"EVENT#{day}"))
    digests = query(Key("pk").eq(f"DIGEST#{day}"), newest_first=True)
    econ = query(Key("pk").eq("ECONCAL") & Key("sk").begins_with(day))
    meta = table.get_item(Key={"pk": f"META#{day}", "sk": "calendar"}).get("Item")
    return {"date": day, "watchlist": SYMBOLS, "news": strip(news), "events": strip(events),
            "digests": strip(digests), "econ": strip(econ),
            "loaded_at": (meta or {}).get("loaded_at")}


def econ_payload(start, end):
    items = query(Key("pk").eq("ECONCAL") & Key("sk").between(start, end + "#~"))
    return {"from": start, "to": end, "econ": strip(items)}


def authorized(headers):
    if not ACCESS_KEY:
        return True
    given = (headers or {}).get("x-access-key", "")
    return hmac.compare_digest(given.encode(), ACCESS_KEY.encode())


def lambda_handler(event, context):
    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    qs = event.get("queryStringParameters") or {}
    if method != "GET":
        return respond(405, {"error": "method not allowed"})
    if path in ("/", "/index.html"):
        return respond(200, INDEX_HTML, "text/html; charset=utf-8", "no-cache")
    if not path.startswith("/api/"):
        return respond(404, {"error": "not found"})
    if not authorized(event.get("headers")):
        return respond(401, {"error": "access key required"})

    today = datetime.now(TZ).date()
    if path == "/api/day":
        day = qs.get("date") or today.isoformat()
        if not DATE_RE.match(day):
            return respond(400, {"error": "date must be YYYY-MM-DD"})
        return respond(200, day_payload(day))
    if path == "/api/econ":
        start = qs.get("from") or (today - timedelta(days=7)).isoformat()
        end = qs.get("to") or (today + timedelta(days=30)).isoformat()
        if not (DATE_RE.match(start) and DATE_RE.match(end)):
            return respond(400, {"error": "from/to must be YYYY-MM-DD"})
        return respond(200, econ_payload(start, end))
    return respond(404, {"error": "not found"})
