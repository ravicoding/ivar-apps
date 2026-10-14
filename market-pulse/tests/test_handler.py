"""Local test: mocked DynamoDB (moto) + fake Finnhub / FRED / Bedrock responses.
Run:  pip install moto boto3 && python tests/test_handler.py
"""
import json, os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.update(TABLE_NAME="news", FINNHUB_API_KEY="x", FRED_API_KEY="y", SYMBOLS="AAPL,MSFT",
                  AWS_DEFAULT_REGION="us-east-1", AWS_ACCESS_KEY_ID="t", AWS_SECRET_ACCESS_KEY="t",
                  BEDROCK_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0", DIGEST_HOURS="8,12,17",
                  ACCESS_KEY="secret123")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from moto import mock_aws

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 12, 12, 5, tzinfo=NY)  # 12:05 ET -> digest slot
T = int(NOW.timestamp())

FINNHUB = {
    "/news": [
        {"id": 1, "datetime": T - 600, "headline": "Stocks rise as yields fall", "summary": "Wall Street " * 60,
         "source": "Reuters", "url": "https://x/1", "related": ""},
        {"id": 2, "datetime": T - 86400 * 3, "headline": "Old story", "summary": "old", "source": "X", "url": "u"},
    ],
    "/company-news": [
        {"id": 3, "datetime": T - 300, "headline": "Apple unveils chip", "summary": "New M-series.",
         "source": "CNBC", "url": "https://x/3", "related": "AAPL"},
    ],
    "/calendar/earnings": {"earningsCalendar": [
        {"symbol": "AAPL", "quarter": 4, "year": 2026, "hour": "amc", "epsEstimate": 1.81,
         "revenueEstimate": 1.02e11, "epsActual": None, "revenueActual": None},
        {"symbol": "JPM", "quarter": 3, "year": 2026, "hour": "bmo", "epsEstimate": 4.9,
         "revenueEstimate": 4.3e10, "epsActual": 5.1, "revenueActual": 4.4e10}]},
    "/calendar/ipo": {"ipoCalendar": [{"name": "Acme Robotics", "symbol": "ACME", "exchange": "NASDAQ",
                                        "status": "expected", "price": "18-20", "totalSharesValue": 2.5e8}]},
}

RELEASE_DATES = [  # what FRED returns (many unrelated releases are ignored)
    {"release_id": 10, "release_name": "Consumer Price Index", "date": "2026-10-14"},
    {"release_id": 50, "release_name": "Employment Situation", "date": "2026-10-16"},
    {"release_id": 46, "release_name": "Producer Price Index", "date": "2026-10-12"},
    {"release_id": 180, "release_name": "Unemployment Insurance Weekly Claims Report", "date": "2026-10-15"},
    {"release_id": 469, "release_name": "State Unemployment Insurance Weekly Claims Report", "date": "2026-10-15"},
    {"release_id": 18, "release_name": "H.15 Selected Interest Rates", "date": "2026-10-13"},
]


def series(start, step, n=14):  # newest first, monthly dates
    obs, y, m = [], 2026, 9
    for i in range(n):
        obs.append({"date": f"{y}-{m:02d}-01", "value": f"{start - i * step:.3f}"})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    obs.append({"date": "2025-07-01", "value": "."})  # missing values must be skipped
    return obs


SERIES = {"CPIAUCSL": series(330.0, 0.9), "CPILFESL": series(335.0, 1.0), "PAYEMS": series(160150, 140),
          "UNRATE": [{"date": "2026-09-01", "value": "4.3"}, {"date": "2026-08-01", "value": "4.2"}],
          "PPIFIS": series(150.0, 0.3), "ICSA": [{"date": "2026-10-03", "value": "231000"}, {"date": "2026-09-26", "value": "226000"}],
          "DFEDTARL": [{"date": "2026-10-09", "value": "3.75"}, {"date": "2026-10-08", "value": "3.75"}],
          "DFEDTARU": [{"date": "2026-10-09", "value": "4.00"}, {"date": "2026-10-08", "value": "4.00"}]}


def fake_get_json(url, params):
    if "finnhub" in url:
        return FINNHUB[url.split("/api/v1")[1]]
    if url.endswith("/releases/dates"):
        assert params["include_release_dates_with_no_data"] == "true"
        return {"count": len(RELEASE_DATES), "release_dates": RELEASE_DATES if params["offset"] == 0 else []}
    if url.endswith("/series/observations"):
        return {"observations": SERIES.get(params["series_id"], [])}
    raise AssertionError(url)


@mock_aws
def main():
    import boto3
    boto3.client("dynamodb").create_table(
        TableName="news", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
    import handler
    calls = []
    handler.get_json = lambda url, params: (calls.append(url), fake_get_json(url, params))[1]
    prompts = []
    handler.bedrock_converse = lambda p: (prompts.append(p), ("Digest: stocks up.", {"inputTokens": 900, "outputTokens": 120}))[1]

    # ---- collector
    r1 = handler.lambda_handler({"now": NOW.isoformat()}, None)
    print("run 1:", r1)
    # 3 seen (same Apple article returned for AAPL and MSFT) -> stored once; old story filtered out
    assert r1["news_seen"] == 3 and r1["news_stored"] == 2
    # 2 earnings + 1 IPO + econ: CPI, NFP, PPI, CLAIMS, FOMC (Oct 28 within 30 days)
    assert r1["events_written"] == 3 + 5, r1
    assert r1["digest_usage"]
    r2 = handler.lambda_handler({"now": NOW.replace(minute=20).isoformat()}, None)
    print("run 2:", r2)
    assert r2["news_stored"] == 0 and r2["events_written"] is None and r2["digest_usage"] is None
    assert sum("calendar/earnings" in c for c in calls) == 1

    t = handler.table
    econ = t.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": "ECONCAL"})["Items"]
    print("\nECONOMIC CALENDAR:")
    for e in econ:
        print(f"  {e['date']} {e['time_et']} [{e['importance']}] {e['summary']}")
    by = {e["key"]: e for e in econ}
    assert set(by) == {"CPI", "NFP", "PPI", "CLAIMS", "FOMC"}
    assert by["NFP"]["values"][0]["latest"] == "+140K jobs" and by["NFP"]["values"][1]["latest"] == "4.3%"
    assert "m/m" in by["CPI"]["values"][0]["latest"] and "y/y" in by["CPI"]["values"][0]["latest"]
    assert by["FOMC"]["values"][0]["latest"] == "3.75–4%" and by["FOMC"]["time_et"] == "14:00"
    assert by["CLAIMS"]["values"][0]["latest"] == "231K"
    assert "PPI" in prompts[0] and "Apple unveils chip" in prompts[0]

    # ---- reader API
    import api
    ev = lambda path, qs=None, key="secret123": {"rawPath": path, "queryStringParameters": qs,
        "headers": {"x-access-key": key} if key else {}, "requestContext": {"http": {"method": "GET"}}}
    page = api.lambda_handler(ev("/", key=None), None)
    assert page["statusCode"] == 200 and "<title>Market Pulse</title>" in page["body"]
    assert api.lambda_handler(ev("/api/day", {"date": "2026-10-12"}, key="wrong"), None)["statusCode"] == 401
    assert api.lambda_handler(ev("/api/day", {"date": "2026-10-12'"}), None)["statusCode"] == 400
    day = json.loads(api.lambda_handler(ev("/api/day", {"date": "2026-10-12"}), None)["body"])
    print(f"\nAPI /api/day: {len(day['news'])} news, {len(day['events'])} events, "
          f"{len(day['digests'])} digests, econ today: {[e['key'] for e in day['econ']]}")
    assert len(day["news"]) == 2 and day["news"][0]["headline"] == "Apple unveils chip"  # newest first
    assert [e["key"] for e in day["econ"]] == ["PPI"] and "pk" not in day["news"][0]
    cal = json.loads(api.lambda_handler(ev("/api/econ", {"from": "2026-10-12", "to": "2026-10-16"}), None)["body"])
    print("API /api/econ:", [(e["date"], e["key"]) for e in cal["econ"]])
    assert [e["key"] for e in cal["econ"]] == ["PPI", "CPI", "CLAIMS", "NFP"]
    print("\nALL CHECKS PASSED")


main()
