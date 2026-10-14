"""Stock news & financial-events collector (AWS Lambda).

Triggered on a schedule by EventBridge Scheduler. Each run:
  1. Pulls general market news and news for your watchlist from Finnhub.
  2. On the first run of the day (and at EVENT_REFRESH_HOURS), pulls today's
     earnings and IPO calendar from Finnhub, and the US economic calendar
     (CPI, nonfarm payrolls, PCE, GDP, PPI, retail sales, JOLTS, jobless
     claims, FOMC) with the latest values from FRED.
  3. Stores a brief summary of each item in DynamoDB. News is de-duplicated
     and every item expires automatically after TTL_DAYS.
  4. Optionally, at DIGEST_HOURS, asks Amazon Bedrock to write a short
     market digest of the day so far and stores that too.

DynamoDB layout (single table, pk + sk):
  NEWS#2026-10-12    2026-10-12T09:41:00#<article id>   one news item
  EVENT#2026-10-12   EARNINGS#AAPL | IPO#XYZ             one calendar event
  ECONCAL            2026-10-15#CPI                      economic release + latest values
  DIGEST#2026-10-12  12:00                               AI digest
  META#2026-10-12    calendar                            bookkeeping marker
"""
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import econ

try:
    from zoneinfo import ZoneInfo
    MARKET_TZ = ZoneInfo(os.environ.get("MARKET_TZ", "America/New_York"))
except Exception:  # pragma: no cover - fallback if tz data is missing
    MARKET_TZ = timezone(timedelta(hours=-5))

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------------------------------------------------------------- settings
TABLE_NAME = os.environ["TABLE_NAME"]
API_KEY = os.environ.get("FINNHUB_API_KEY", "")
SYMBOLS = [s.strip().upper() for s in os.environ.get("SYMBOLS", "").split(",") if s.strip()]
NEWS_CATEGORIES = [c.strip() for c in os.environ.get("NEWS_CATEGORIES", "general").split(",") if c.strip()]
SCHEDULE_MINUTES = int(os.environ.get("SCHEDULE_MINUTES", "15"))
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "90"))
FIRST_RUN_LOOKBACK_HOURS = int(os.environ.get("FIRST_RUN_LOOKBACK_HOURS", "16"))
TTL_DAYS = int(os.environ.get("TTL_DAYS", "30"))
EVENT_REFRESH_HOURS = {int(h) for h in os.environ.get("EVENT_REFRESH_HOURS", "9,11,17").split(",") if h.strip()}
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "").strip()
DIGEST_HOURS = {int(h) for h in os.environ.get("DIGEST_HOURS", "8,12,17").split(",") if h.strip()}
SUMMARY_CHARS = 280

FINNHUB = "https://finnhub.io/api/v1"
table = boto3.resource("dynamodb").Table(TABLE_NAME)
_bedrock = None


# ---------------------------------------------------------------- helpers
def http_get(path, params):
    """GET a Finnhub endpoint."""
    return get_json(f"{FINNHUB}{path}", {**params, "token": API_KEY})


def get_json(base_url, params):
    """GET a URL and return parsed JSON (retries on 429/5xx)."""
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return None


def brief(text, limit=SUMMARY_CHARS):
    """Collapse whitespace and trim to a short summary at a word boundary."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def num(value):
    """Floats must be Decimal for DynamoDB; skip None."""
    return None if value is None else Decimal(str(value))


def clean(item):
    """Drop None / empty values so DynamoDB stays tidy."""
    return {k: v for k, v in item.items() if v not in (None, "", [])}


def ttl_epoch(now):
    return int((now + timedelta(days=TTL_DAYS)).timestamp())


def put_if_new(item):
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


# ---------------------------------------------------------------- news
def news_item(article, category, now):
    published = datetime.fromtimestamp(article.get("datetime", 0), tz=timezone.utc).astimezone(MARKET_TZ)
    related = [s for s in (article.get("related") or "").split(",") if s]
    return clean({
        "pk": f"NEWS#{published:%Y-%m-%d}",
        "sk": f"{published:%Y-%m-%dT%H:%M:%S}#{article.get('id')}",
        "type": "news",
        "category": category,
        "headline": brief(article.get("headline"), 200),
        "summary": brief(article.get("summary")),
        "source": article.get("source"),
        "url": article.get("url"),
        "symbols": related,
        "published_at": published.isoformat(),
        "collected_at": now.isoformat(),
        "ttl": ttl_epoch(now),
    })


def collect_news(now, since):
    stored = seen = 0
    batches = [(c, http_get("/news", {"category": c}) or []) for c in NEWS_CATEGORIES]
    today = now.strftime("%Y-%m-%d")
    start = since.strftime("%Y-%m-%d")
    for sym in SYMBOLS:
        try:
            batches.append((f"company:{sym}", http_get("/company-news", {"symbol": sym, "from": start, "to": today}) or []))
        except urllib.error.HTTPError as e:
            log.warning("company news %s failed: %s", sym, e)
    cutoff = since.timestamp()
    for category, articles in batches:
        for a in articles:
            if a.get("datetime", 0) < cutoff or not a.get("headline"):
                continue
            seen += 1
            if put_if_new(news_item(a, category, now)):
                stored += 1
    return seen, stored


# ---------------------------------------------------------------- events
HOUR_LABEL = {"bmo": "before market open", "amc": "after market close", "dmh": "during market hours"}


def fmt_money(v):
    if v is None:
        return "n/a"
    v = float(v)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if abs(v) >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.2f}"


def earnings_items(rows, day, now):
    for r in rows:
        sym = r.get("symbol")
        if not sym:
            continue
        when = HOUR_LABEL.get(r.get("hour"), "time TBA")
        text = f"{sym} reports Q{r.get('quarter')} {r.get('year')} earnings {when}. EPS est {r.get('epsEstimate', 'n/a')}"
        if r.get("epsActual") is not None:
            text += f", actual {r['epsActual']}"
        text += f"; revenue est {fmt_money(r.get('revenueEstimate'))}"
        if r.get("revenueActual") is not None:
            text += f", actual {fmt_money(r['revenueActual'])}"
        yield clean({
            "pk": f"EVENT#{day}", "sk": f"EARNINGS#{sym}", "type": "earnings",
            "symbol": sym, "summary": text + ".", "watchlist": sym in SYMBOLS,
            "eps_estimate": num(r.get("epsEstimate")), "eps_actual": num(r.get("epsActual")),
            "revenue_estimate": num(r.get("revenueEstimate")), "revenue_actual": num(r.get("revenueActual")),
            "collected_at": now.isoformat(), "ttl": ttl_epoch(now),
        })


def ipo_items(rows, day, now):
    for r in rows:
        name = r.get("name") or r.get("symbol")
        if not name:
            continue
        text = (f"IPO: {name} ({r.get('symbol') or 'TBA'}) on {r.get('exchange') or 'TBA'}, "
                f"status {r.get('status') or 'n/a'}, price {r.get('price') or 'TBA'}, "
                f"raising {fmt_money(r.get('totalSharesValue'))}.")
        yield clean({
            "pk": f"EVENT#{day}", "sk": f"IPO#{r.get('symbol') or name}", "type": "ipo",
            "symbol": r.get("symbol"), "summary": text,
            "collected_at": now.isoformat(), "ttl": ttl_epoch(now),
        })


def collect_events(now):
    day = now.strftime("%Y-%m-%d")
    items = []
    earn = http_get("/calendar/earnings", {"from": day, "to": day}) or {}
    items += list(earnings_items(earn.get("earningsCalendar", []), day, now))
    ipo = http_get("/calendar/ipo", {"from": day, "to": day}) or {}
    items += list(ipo_items(ipo.get("ipoCalendar", []), day, now))
    with table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as bw:
        for it in items:
            bw.put_item(Item=it)  # overwrite so actuals fill in on later refreshes
    econ_count = 0
    if FRED_API_KEY:
        try:
            econ_count = econ.collect(table, get_json, FRED_API_KEY, now.date())
        except Exception as e:  # keep news/earnings working even if FRED is down
            log.error("economic calendar failed: %s", e)
    table.put_item(Item={"pk": f"META#{day}", "sk": "calendar", "loaded_at": now.isoformat(), "ttl": ttl_epoch(now)})
    return len(items) + econ_count


# ---------------------------------------------------------------- digest
def bedrock_converse(prompt):
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime")
    resp = _bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 500, "temperature": 0.2},
    )
    usage = resp.get("usage", {})
    return resp["output"]["message"]["content"][0]["text"], usage


def query_all(pk, **kw):
    out, kwargs = [], {"KeyConditionExpression": Key("pk").eq(pk), **kw}
    while True:
        r = table.query(**kwargs)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def write_digest(now):
    day = now.strftime("%Y-%m-%d")
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    news = query_all(f"NEWS#{day}")
    news += [n for n in query_all(f"NEWS#{yday}") if n["sk"] >= f"{yday}T16:00"]
    news = sorted(news, key=lambda n: n["sk"], reverse=True)[:120]
    events = query_all("ECONCAL", KeyConditionExpression=Key("pk").eq("ECONCAL") & Key("sk").begins_with(day))
    events += [e for e in query_all(f"EVENT#{day}") if e.get("watchlist") or e["type"] != "earnings"]
    if not news and not events:
        return None
    lines = [f"- [{n.get('source', '')}] {n.get('headline', '')}: {n.get('summary', '')}" for n in news]
    ev = [f"- {e['summary']}" for e in events[:60]]
    prompt = (
        f"You are a markets editor. It is {now:%A %B %d, %Y %H:%M} US Eastern. Using ONLY the items below, "
        "write a brief market digest: one headline sentence, then 4-6 bullets on the most market-moving "
        "stories, then one line on today's economic releases (CPI, jobs, Fed, etc.) if any, "
        f"then one line on scheduled events for the watchlist ({', '.join(SYMBOLS) or 'none'}). "
        "Max 190 words. No investment advice.\n\n"
        "NEWS:\n" + "\n".join(lines) + "\n\nEVENTS:\n" + ("\n".join(ev) or "- none")
    )
    text, usage = bedrock_converse(prompt)
    table.put_item(Item=clean({
        "pk": f"DIGEST#{day}", "sk": f"{now:%H:%M}", "type": "digest", "summary": text.strip(),
        "model": BEDROCK_MODEL_ID, "news_count": len(news),
        "input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"),
        "collected_at": now.isoformat(), "ttl": ttl_epoch(now),
    }))
    return usage


# ---------------------------------------------------------------- entry point
def lambda_handler(event, context):
    now = datetime.now(MARKET_TZ)
    if isinstance(event, dict) and event.get("now"):  # manual override for testing/backfill
        now = datetime.fromisoformat(event["now"]).astimezone(MARKET_TZ)
    day = now.strftime("%Y-%m-%d")
    first_run = "Item" not in table.get_item(Key={"pk": f"META#{day}", "sk": "calendar"})
    in_slot = now.minute < SCHEDULE_MINUTES  # the first scheduled run within an hour

    since = now - (timedelta(hours=FIRST_RUN_LOOKBACK_HOURS) if first_run else timedelta(minutes=LOOKBACK_MINUTES))
    seen, stored = collect_news(now, since)

    events = None
    if first_run or (in_slot and now.hour in EVENT_REFRESH_HOURS):
        events = collect_events(now)

    digest = None
    if BEDROCK_MODEL_ID and in_slot and now.hour in DIGEST_HOURS:
        try:
            digest = write_digest(now)
        except ClientError as e:
            log.error("digest failed: %s", e)

    result = {"time": now.isoformat(), "news_seen": seen, "news_stored": stored,
              "events_written": events, "digest_usage": digest}
    log.info(json.dumps(result, default=str))
    return result
