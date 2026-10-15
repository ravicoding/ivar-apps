"""
Stock Price Alerts - AWS Lambda
-------------------------------
One function, two jobs:
  1. EventBridge Scheduler calls it every 5 minutes, 9:30 AM - 4:00 PM ET,
     Monday - Friday. When a price leaves its low-high range (or a single
     target is hit) it texts your phone (Amazon SNS) and, if you set a topic
     on the page, also sends an ntfy app push.
  2. Its Function URL is the JSON API for the settings page, which is a
     static file hosted on S3 (see web/). CORS is set in template.yaml.

No third-party packages: only the Python standard library and boto3,
which the Lambda runtime already includes.
"""

import base64
import hmac
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
PHONE_NUMBER = os.environ.get("PHONE_NUMBER", "").strip()            # E.164, e.g. +15551234567
SMS_ORIGINATION = os.environ.get("SMS_ORIGINATION_NUMBER", "").strip()  # your registered toll-free number
SNS = boto3.client("sns")
SETTINGS_ID = "__settings__"
SCHEDULE_TEXT = "Every 5 min, 9:30 AM - 4:00 PM ET, Mon - Fri"


# ------------------------------------------------------------------ helpers

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_ddb(obj):
    """Python floats -> Decimal for DynamoDB."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def from_ddb(obj):
    """DynamoDB Decimals -> plain numbers for JSON."""
    if isinstance(obj, list):
        return [from_ddb(x) for x in obj]
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


def get_settings():
    item = TABLE.get_item(Key={"id": SETTINGS_ID}).get("Item") or {}
    s = from_ddb(item)
    s.setdefault("ntfy_topic", "")
    s.setdefault("last_run", None)
    s.setdefault("last_error", None)
    s.pop("id", None)
    return s


def update_settings(**fields):
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in to_ddb(fields).items()}
    TABLE.update_item(
        Key={"id": SETTINGS_ID},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def list_alerts():
    items, kwargs = [], {}
    while True:
        resp = TABLE.scan(**kwargs)
        items += resp.get("Items", [])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    alerts = [from_ddb(i) for i in items if i["id"] != SETTINGS_ID]
    alerts.sort(key=lambda a: a.get("created_at", ""))
    return alerts


# ------------------------------------------------------------------ prices

def get_price(ticker):
    """Latest price from Yahoo Finance's chart endpoint, or None."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote(ticker)}?interval=1m&range=1d")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    price = result[0].get("meta", {}).get("regularMarketPrice")
    return float(price) if price else None


def get_prices(tickers):
    prices, errors = {}, []

    def one(tk):
        try:
            return tk, get_price(tk), None
        except Exception as e:  # network, throttling, bad JSON
            return tk, None, str(e)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for tk, p, err in pool.map(one, tickers):
            if p is not None:
                prices[tk] = p
            else:
                errors.append(f"{tk}: {err or 'no price returned'}")
    return prices, errors


def hit_target(price, target, direction):
    return price >= target if direction == "above" else price <= target


def zone_of(price, low, high):
    """Where a price sits against a range: "below", "above" or "inside"."""
    if low is not None and price <= low:
        return "below"
    if high is not None and price >= high:
        return "above"
    return "inside"


def range_text(low, high):
    if low is not None and high is not None:
        return f"${low:,.2f} - ${high:,.2f}"
    return f"at least ${low:,.2f}" if low is not None else f"at most ${high:,.2f}"


# ------------------------------------------------------------------ push

def masked_phone():
    digits = "".join(c for c in PHONE_NUMBER if c.isdigit())
    return f"(•••) •••-{digits[-4:]}" if len(digits) >= 4 else ""


def send_sms(message):
    """Text PHONE_NUMBER through Amazon SNS. Returns an error string or None."""
    if not PHONE_NUMBER:
        return "No phone number set (PhoneNumber parameter)."
    attrs = {"AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"}}
    if SMS_ORIGINATION:
        attrs["AWS.MM.SMS.OriginationNumber"] = {"DataType": "String", "StringValue": SMS_ORIGINATION}
    try:
        SNS.publish(PhoneNumber=PHONE_NUMBER, Message=message, MessageAttributes=attrs)
    except Exception as e:
        return f"Text message failed: {e}"
    return None


def notify(topic, title, message, tags="bell"):
    """Send through every configured channel. Returns (sent_channels, errors)."""
    sent, errors = [], []
    if PHONE_NUMBER:
        err = send_sms(f"Price alert: {message}")
        (errors.append(err) if err else sent.append("text"))
    if topic:
        err = push(topic, title, message, tags)
        (errors.append(err) if err else sent.append("app push"))
    if not PHONE_NUMBER and not topic:
        errors.append("No notification channel is set up.")
    return sent, errors


def push(topic, title, message, tags="bell"):
    """Send an app notification through ntfy. Returns an error string or None."""
    if not topic:
        return "Add your ntfy topic first."
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": "high", "Tags": tags},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception as e:
        return f"Couldn't reach ntfy: {e}"
    return None


# ------------------------------------------------------------------ the check

def run_check():
    alerts = [a for a in list_alerts() if a["status"] == "watching"]
    tickers = sorted({a["ticker"] for a in alerts})
    prices, errors = get_prices(tickers) if tickers else ({}, [])
    topic = get_settings()["ntfy_topic"]
    stamp = now_iso()
    hits = 0

    for a in alerts:
        p = prices.get(a["ticker"])
        if p is None:
            continue
        if a.get("kind") == "range":
            if check_range(a, p, stamp, topic, errors):
                hits += 1
            continue
        if hit_target(p, a["target"], a["direction"]):
            try:
                # Conditional write: only the first run to see the hit sends the push.
                TABLE.update_item(
                    Key={"id": a["id"]},
                    UpdateExpression="SET #s = :t, triggered_at = :ts, triggered_price = :p, "
                                     "last_price = :p, last_checked = :ts",
                    ConditionExpression=Attr("status").eq("watching"),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues=to_ddb({":t": "triggered", ":ts": stamp, ":p": p}),
                )
            except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
                continue
            hits += 1
            word = "rose to" if a["direction"] == "above" else "fell to"
            tags = "chart_with_upwards_trend" if a["direction"] == "above" else "chart_with_downwards_trend"
            _, errs = notify(
                topic,
                f"{a['ticker']} {word} ${p:,.2f}",
                f"{a['ticker']} is ${p:,.2f}, {a['direction']} your target of ${a['target']:,.2f}.",
                tags,
            )
            errors += errs
        else:
            TABLE.update_item(
                Key={"id": a["id"]},
                UpdateExpression="SET last_price = :p, last_checked = :ts",
                ExpressionAttributeValues=to_ddb({":p": p, ":ts": stamp}),
            )

    update_settings(last_run=stamp, last_error="; ".join(errors) or None)
    print(json.dumps({"checked": len(tickers), "hits": hits, "errors": errors}))
    return {"checked": len(tickers), "hits": hits, "errors": errors}


def check_range(a, p, stamp, topic, errors):
    """Alert once each time the price leaves the range. Returns True if it alerted."""
    old, new = a.get("zone", "inside"), zone_of(p, a.get("low"), a.get("high"))
    if new == old:
        TABLE.update_item(
            Key={"id": a["id"]},
            UpdateExpression="SET last_price = :p, last_checked = :ts",
            ExpressionAttributeValues=to_ddb({":p": p, ":ts": stamp}),
        )
        return False
    fields = {":z": new, ":old": old, ":p": p, ":ts": stamp}
    update = "SET #z = :z, last_price = :p, last_checked = :ts"
    if new != "inside":
        update += ", triggered_at = :ts, triggered_price = :p, alerts_sent = if_not_exists(alerts_sent, :zero) + :one"
        fields.update({":zero": 0, ":one": 1})
    try:
        # Conditional write: only the first run to see the zone change sends the push.
        TABLE.update_item(
            Key={"id": a["id"]},
            UpdateExpression=update,
            ConditionExpression=Attr("zone").eq(old) | Attr("zone").not_exists(),
            ExpressionAttributeNames={"#z": "zone"},
            ExpressionAttributeValues=to_ddb(fields),
        )
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    if new == "inside":
        return False
    rng = range_text(a.get("low"), a.get("high"))
    if new == "below":
        title, tags = f"{a['ticker']} fell to ${p:,.2f}", "chart_with_downwards_trend"
        message = f"{a['ticker']} is ${p:,.2f}, at or below your low of ${a['low']:,.2f} (range {rng})."
    else:
        title, tags = f"{a['ticker']} rose to ${p:,.2f}", "chart_with_upwards_trend"
        message = f"{a['ticker']} is ${p:,.2f}, at or above your high of ${a['high']:,.2f} (range {rng})."
    _, errs = notify(topic, title, message, tags)
    errors += errs
    return True


# ------------------------------------------------------------------ HTTP

def respond(status, body, content_type="application/json"):
    if content_type == "application/json":
        body = json.dumps(body)
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type, "Cache-Control": "no-store"},
        "body": body,
    }


def error(status, message):
    return respond(status, {"error": message})


def authorized(headers):
    given = headers.get("x-app-password", "")
    return bool(APP_PASSWORD) and hmac.compare_digest(given.encode(), APP_PASSWORD.encode())


def read_json(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def state_payload():
    return {
        "settings": get_settings(),
        "alerts": list_alerts(),
        "schedule": SCHEDULE_TEXT,
        "sms": {"to": masked_phone(), "registered_sender": bool(SMS_ORIGINATION)},
    }


def parse_bound(value, name):
    """Optional price: blank means no bound. Raises ValueError with a friendly message."""
    if value is None or str(value).strip() == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter the {name} price as a number, like 185 or 18.75.")
    if not n > 0 or n != n or n == float("inf"):
        raise ValueError(f"The {name} price must be above zero.")
    return round(n, 4)


def create_range_alert(body):
    ticker = str(body.get("ticker", "")).strip().upper()
    if not ticker or len(ticker) > 15:
        return error(400, "Enter a ticker symbol, like AAPL.")
    try:
        low, high = parse_bound(body.get("low"), "low"), parse_bound(body.get("high"), "high")
    except ValueError as e:
        return error(400, str(e))
    if low is None and high is None:
        return error(400, "Enter a low price, a high price, or both.")
    if low is not None and high is not None and low >= high:
        return error(400, "The low price must be below the high price.")
    try:
        price = get_price(ticker)
    except Exception as e:
        return error(502, f"Couldn't look up {ticker} right now ({e}). Try again in a minute.")
    if price is None:
        return error(400, f"Couldn't find a price for {ticker}. Check the symbol (use BRK-B, not BRK.B).")
    stamp = now_iso()
    alert = {
        "id": uuid.uuid4().hex[:10],
        "kind": "range",
        "ticker": ticker,
        "low": low,
        "high": high,
        # Start "inside" so a price already outside the range alerts on the next check.
        "zone": "inside",
        "status": "watching",
        "alerts_sent": 0,
        "created_at": stamp,
        "start_price": price,
        "last_price": price,
        "last_checked": stamp,
    }
    TABLE.put_item(Item=to_ddb({k: v for k, v in alert.items() if v is not None}))
    return respond(201, {**alert, "now_outside": zone_of(price, low, high)})


def handle_http(event):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    if method == "GET" and path == "/":
        return respond(200, {"ok": True, "service": "price-alerts"})

    if not path.startswith("/api/"):
        return error(404, "Not found.")
    if not authorized(headers):
        return error(401, "Wrong password.")

    if method == "GET" and path == "/api/state":
        return respond(200, state_payload())

    if method == "POST" and path == "/api/alerts":
        body = read_json(event)
        if "low" in body or "high" in body:
            return create_range_alert(body)
        ticker = str(body.get("ticker", "")).strip().upper()
        direction = body.get("direction", "auto")
        try:
            target = float(body.get("target"))
        except (TypeError, ValueError):
            return error(400, "Enter a target price, like 250 or 18.75.")
        if not ticker or len(ticker) > 15:
            return error(400, "Enter a ticker symbol, like AAPL.")
        if target <= 0:
            return error(400, "Target price must be above zero.")
        if direction not in ("auto", "above", "below"):
            return error(400, "Direction must be auto, above or below.")
        try:
            price = get_price(ticker)
        except Exception as e:
            return error(502, f"Couldn't look up {ticker} right now ({e}). Try again in a minute.")
        if price is None:
            return error(400, f"Couldn't find a price for {ticker}. Check the symbol (use BRK-B, not BRK.B).")
        if direction == "auto":
            direction = "above" if target >= price else "below"
        stamp = now_iso()
        alert = {
            "id": uuid.uuid4().hex[:10],
            "ticker": ticker,
            "target": target,
            "direction": direction,
            "status": "watching",
            "created_at": stamp,
            "start_price": price,
            "last_price": price,
            "last_checked": stamp,
        }
        TABLE.put_item(Item=to_ddb(alert))
        return respond(201, alert)

    if method == "DELETE" and path.startswith("/api/alerts/"):
        alert_id = path.rsplit("/", 1)[-1]
        if alert_id == SETTINGS_ID:
            return error(400, "That isn't an alert.")
        TABLE.delete_item(Key={"id": alert_id})
        return respond(200, {"ok": True})

    if method == "POST" and path == "/api/settings":
        topic = str(read_json(event).get("ntfy_topic", "")).strip()
        if topic and not all(c.isalnum() or c in "-_" for c in topic):
            return error(400, "Use only letters, numbers, dashes and underscores in the topic.")
        update_settings(ntfy_topic=topic)
        return respond(200, {"ntfy_topic": topic})

    if method == "POST" and path == "/api/topic/generate":
        return respond(200, {"topic": "stocks-" + secrets.token_hex(5)})

    if method == "POST" and path == "/api/test":
        sent, errs = notify(get_settings()["ntfy_topic"], "Price alerts connected",
                            "Test message. Your stock alerts will arrive like this.", "white_check_mark")
        if errs and not sent:
            return error(400, "; ".join(errs))
        return respond(200, {"sent": sent, "errors": errs})

    if method == "POST" and path == "/api/check":
        result = run_check()
        return respond(200, {**result, **state_payload()})

    return error(404, "Not found.")


# ------------------------------------------------------------------ entry point

def handler(event, context):
    if isinstance(event, dict) and "requestContext" in event:
        return handle_http(event)
    return run_check()  # scheduled run
