import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["ALLOWED_EMAILS"] = "Owner@Example.com, second@example.com"
os.environ["CLIENT_ID"] = "client123"
os.environ["AUTH_DOMAIN"] = "auth.example.com"

import app  # noqa: E402


class FakeTable:
    def __init__(self, items=()):
        self.items = {i["id"]: dict(i) for i in items}

    def scan(self, **_):
        return {"Items": [dict(i) for i in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["id"], None)


OWNER = {"email": "owner@example.com", "email_verified": "true", "token_use": "id"}


def call(table, method, path, body=None, claims=OWNER, query=None):
    ctx = {"http": {"method": method}}
    if claims is not None:
        ctx["authorizer"] = {"jwt": {"claims": claims}}
    event = {"requestContext": ctx, "rawPath": path, "headers": {},
             "body": json.dumps(body) if body is not None else None,
             "queryStringParameters": query}
    with mock.patch.object(app, "TABLE", table):
        r = app.handler(event, None)
    ctype = r["headers"]["Content-Type"]
    return r["statusCode"], json.loads(r["body"]) if ctype == "application/json" else r["body"]


TRADE = {"ticker": "aapl", "quantity": "10", "buy_price": "150.25", "buy_date": "2026-01-05",
         "sell_price": "160.75", "sell_date": "2026-02-10", "notes": "earnings run"}


class TradeTests(unittest.TestCase):
    def test_create_closed_trade(self):
        t = FakeTable()
        status, body = call(t, "POST", "/api/trades", TRADE)
        self.assertEqual(status, 201)
        self.assertEqual(body["ticker"], "AAPL")
        self.assertEqual((body["cost"], body["proceeds"], body["pnl"], body["status"]), (1502.5, 1607.5, 105.0, "closed"))
        self.assertEqual(len(t.items), 1)

    def test_open_position(self):
        status, body = call(FakeTable(), "POST", "/api/trades", {**TRADE, "sell_price": "", "sell_date": ""})
        self.assertEqual((status, body["status"]), (201, "open"))
        self.assertNotIn("pnl", body)

    def test_fractional_and_formatted_numbers(self):
        status, body = call(FakeTable(), "POST", "/api/trades", {**TRADE, "quantity": "0.5", "buy_price": "$1,000", "sell_price": "1100"})
        self.assertEqual((status, body["pnl"]), (201, 50.0))

    def test_validation(self):
        cases = [
            ({"ticker": ""}, "ticker"),
            ({"quantity": "0"}, "above zero"),
            ({"buy_price": "abc"}, "number"),
            ({"buy_date": "10/05/2026"}, "date like"),
            ({"buy_date": "2999-01-01"}, "future"),
            ({"sell_date": ""}, "both"),
            ({"sell_date": "2025-12-31"}, "before the buy date"),
            ({"notes": "x" * 501}, "Notes"),
        ]
        for change, words in cases:
            status, body = call(FakeTable(), "POST", "/api/trades", {**TRADE, **change})
            self.assertEqual(status, 400, change)
            self.assertIn(words, body["error"], change)

    def test_update_keeps_created_at(self):
        t = FakeTable()
        _, created = call(t, "POST", "/api/trades", TRADE)
        status, body = call(t, "PUT", f"/api/trades/{created['id']}", {**TRADE, "sell_price": "140.25"})
        self.assertEqual((status, body["pnl"]), (200, -100.0))
        self.assertEqual(t.items[created["id"]]["created_at"], created["created_at"])
        self.assertEqual(len(t.items), 1)

    def test_update_missing_and_delete(self):
        t = FakeTable()
        self.assertEqual(call(t, "PUT", "/api/trades/0123456789ab", TRADE)[0], 404)
        self.assertEqual(call(t, "PUT", "/api/trades/../x", TRADE)[0], 404)
        _, created = call(t, "POST", "/api/trades", TRADE)
        self.assertEqual(call(t, "DELETE", f"/api/trades/{created['id']}")[0], 200)
        self.assertEqual(t.items, {})

    def test_list_newest_first(self):
        t = FakeTable()
        call(t, "POST", "/api/trades", TRADE)
        call(t, "POST", "/api/trades", {**TRADE, "ticker": "MSFT", "sell_date": "2026-03-01"})
        call(t, "POST", "/api/trades", {**TRADE, "ticker": "NVDA", "sell_price": "", "sell_date": "", "buy_date": "2026-01-02"})
        _, body = call(t, "GET", "/api/trades")
        self.assertEqual([x["ticker"] for x in body["trades"]], ["MSFT", "AAPL", "NVDA"])


CALL = {"asset": "option", "ticker": "aapl", "option_type": "Call", "side": "long", "strike": "200",
        "expiration": "2026-03-20", "quantity": "2", "buy_price": "3.10", "buy_date": "2026-01-05",
        "sell_price": "5.00", "sell_date": "2026-02-10"}


class OptionTests(unittest.TestCase):
    def test_long_call_uses_100_share_multiplier(self):
        t = FakeTable()
        status, body = call(t, "POST", "/api/trades", CALL)
        self.assertEqual(status, 201)
        self.assertEqual((body["asset"], body["option_type"], body["side"], body["strike"]), ("option", "call", "long", 200))
        self.assertEqual((body["cost"], body["proceeds"], body["pnl"]), (620.0, 1000.0, 380.0))  # 2 × 100 × (5.00 − 3.10)

    def test_short_put_expired_worthless(self):
        status, body = call(FakeTable(), "POST", "/api/trades",
                            {**CALL, "option_type": "put", "side": "short", "buy_price": "2.50", "sell_price": "0", "sell_date": "2026-03-20", "quantity": "1"})
        self.assertEqual(status, 201)
        self.assertEqual((body["cost"], body["pnl"], body["pnl_pct"], body["status"]), (250.0, 250.0, 100.0, "closed"))

    def test_short_call_bought_back_at_a_loss(self):
        _, body = call(FakeTable(), "POST", "/api/trades", {**CALL, "side": "short", "buy_price": "1.00", "sell_price": "2.50", "quantity": "1"})
        self.assertEqual((body["pnl"], body["pnl_pct"]), (-150.0, -150.0))

    def test_open_option_may_expire_in_future(self):
        status, body = call(FakeTable(), "POST", "/api/trades", {**CALL, "expiration": "2099-01-15", "sell_price": "", "sell_date": ""})
        self.assertEqual((status, body["status"], body["cost"]), (201, "open", 620.0))

    def test_option_validation(self):
        cases = [
            ({"option_type": ""}, "call or put"),
            ({"side": "sideways"}, "long or short"),
            ({"strike": ""}, "strike"),
            ({"expiration": ""}, "expiration"),
            ({"expiration": "2026-01-01"}, "opening date can't be after"),
            ({"sell_date": "2026-04-01"}, "closing date can't be after"),
            ({"sell_price": "-1"}, "negative"),
            ({"sell_date": ""}, "closing price and the closing date"),
            ({"asset": "crypto"}, "stock or option"),
        ]
        for change, words in cases:
            status, body = call(FakeTable(), "POST", "/api/trades", {**CALL, **change})
            self.assertEqual(status, 400, change)
            self.assertIn(words, body["error"], change)

    def test_stock_sell_price_still_must_be_above_zero(self):
        status, body = call(FakeTable(), "POST", "/api/trades", {**TRADE, "sell_price": "0"})
        self.assertEqual(status, 400)
        self.assertIn("above zero", body["error"])

    def test_switching_option_to_stock_drops_option_fields(self):
        t = FakeTable()
        _, created = call(t, "POST", "/api/trades", CALL)
        call(t, "PUT", f"/api/trades/{created['id']}", {**CALL, "asset": "stock", "buy_price": "150", "sell_price": "160"})
        stored = t.items[created["id"]]
        self.assertEqual(stored["asset"], "stock")
        self.assertNotIn("strike", stored)
        self.assertNotIn("expiration", stored)

    def test_old_trades_without_asset_are_stocks(self):
        old = {"id": "0123456789ab", "ticker": "MSFT", "quantity": 1, "buy_price": 100, "buy_date": "2026-01-02",
               "sell_price": 110, "sell_date": "2026-02-02"}
        _, body = call(FakeTable([old]), "GET", "/api/trades")
        self.assertEqual((body["trades"][0]["asset"], body["trades"][0]["pnl"]), ("stock", 10.0))

    def test_report_mixes_stocks_and_options(self):
        t = FakeTable()
        call(t, "POST", "/api/trades", TRADE)                                   # Feb stock +105
        call(t, "POST", "/api/trades", CALL)                                    # Feb long call +380
        call(t, "POST", "/api/trades", {**CALL, "side": "short", "buy_price": "1.00", "sell_price": "2.50", "quantity": "1"})  # Feb -150
        call(t, "POST", "/api/trades", {**CALL, "side": "short", "sell_price": "", "sell_date": ""})  # open short: no cost
        call(t, "POST", "/api/trades", {**CALL, "sell_price": "", "sell_date": ""})                   # open long: 620 cost
        _, r = call(t, "GET", "/api/report", query={"year": "2026"})
        feb = r["months"][1]
        self.assertEqual((feb["trades"], feb["wins"], feb["losses"], feb["pnl"]), (3, 2, 1, 335.0))
        self.assertEqual(feb["cost"], 1502.5 + 620 + 100)
        self.assertAlmostEqual(feb["proceeds"] - feb["cost"], feb["pnl"])
        self.assertEqual((r["open"]["positions"], r["open"]["cost"]), (2, 620.0))


class ReportTests(unittest.TestCase):
    def test_monthly_totals(self):
        t = FakeTable()
        call(t, "POST", "/api/trades", TRADE)                                                   # Feb +105
        call(t, "POST", "/api/trades", {**TRADE, "sell_price": "140.25"})                       # Feb -100
        call(t, "POST", "/api/trades", {**TRADE, "quantity": "3", "sell_price": "150.15", "sell_date": "2026-05-20"})  # May -0.30
        call(t, "POST", "/api/trades", {**TRADE, "sell_date": "2025-12-30", "buy_date": "2025-12-01"})  # other year
        call(t, "POST", "/api/trades", {**TRADE, "sell_price": "", "sell_date": ""})            # open
        status, r = call(t, "GET", "/api/report", query={"year": "2026"})
        self.assertEqual(status, 200)
        feb, may = r["months"][1], r["months"][4]
        self.assertEqual((feb["trades"], feb["wins"], feb["losses"], feb["pnl"]), (2, 1, 1, 5.0))
        self.assertEqual(may["pnl"], -0.3)
        self.assertEqual(r["months"][0]["trades"], 0)
        self.assertEqual((r["total"]["trades"], r["total"]["pnl"]), (3, 4.7))
        self.assertEqual(r["years"], ["2026", "2025"])
        self.assertEqual((r["open"]["positions"], r["open"]["cost"]), (1, 1502.5))

    def test_default_year_is_latest_with_sales(self):
        t = FakeTable()
        call(t, "POST", "/api/trades", {**TRADE, "sell_date": "2025-12-30", "buy_date": "2025-12-01"})
        self.assertEqual(call(t, "GET", "/api/report")[1]["year"], "2025")

    def test_bad_year(self):
        self.assertEqual(call(FakeTable(), "GET", "/api/report", query={"year": "abc"})[0], 400)


class HttpTests(unittest.TestCase):
    def test_sign_in_required(self):
        self.assertEqual(call(FakeTable(), "GET", "/api/trades", claims=None)[0], 401)
        self.assertEqual(call(FakeTable(), "GET", "/api/trades", claims={**OWNER, "email_verified": "false"})[0], 401)
        self.assertEqual(call(FakeTable(), "GET", "/api/trades", claims={**OWNER, "token_use": "access"})[0], 401)

    def test_only_allowed_emails(self):
        status, body = call(FakeTable(), "GET", "/api/trades", claims={**OWNER, "email": "someone@else.com"})
        self.assertEqual(status, 403)
        self.assertIn("someone@else.com", body["error"])
        self.assertEqual(call(FakeTable(), "GET", "/api/trades", claims={**OWNER, "email": "SECOND@example.com"})[0], 200)
        self.assertEqual(call(FakeTable(), "GET", "/api/me")[1], {"email": "owner@example.com"})

    def test_page_and_health_are_public(self):
        status, html = call(FakeTable(), "GET", "/", claims=None)
        self.assertEqual(status, 200)
        self.assertIn("<title>", html)
        self.assertIn('{"clientId": "client123", "authDomain": "auth.example.com"}', html)
        self.assertNotIn("SIGN_IN_CONFIG", html)
        self.assertEqual(call(FakeTable(), "GET", "/health", claims=None), (200, {"ok": True}))

    def test_errors_are_hidden(self):
        broken = mock.Mock()
        broken.scan.side_effect = RuntimeError("secret detail")
        status, body = call(broken, "GET", "/api/trades")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", body["error"])

    def test_bad_json(self):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/api/trades",
                 "headers": {}, "body": "[1,2"}
        event["requestContext"]["authorizer"] = {"jwt": {"claims": OWNER}}
        with mock.patch.object(app, "TABLE", FakeTable()):
            self.assertEqual(app.handler(event, None)["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
