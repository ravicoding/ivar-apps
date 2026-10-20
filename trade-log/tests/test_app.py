import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["APP_PASSWORD"] = "secret-pass"

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


def call(table, method, path, body=None, password="secret-pass", query=None):
    event = {"requestContext": {"http": {"method": method}}, "rawPath": path,
             "headers": {"X-App-Password": password} if password else {},
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
    def test_password_required(self):
        self.assertEqual(call(FakeTable(), "GET", "/api/trades", password="")[0], 401)
        self.assertEqual(call(FakeTable(), "GET", "/api/trades", password="wrong")[0], 401)

    def test_page_and_health_are_public(self):
        status, html = call(FakeTable(), "GET", "/", password="")
        self.assertEqual(status, 200)
        self.assertIn("<title>", html)
        self.assertTrue(call(FakeTable(), "GET", "/health", password="")[1]["auth_required"])

    def test_no_password_mode(self):
        with mock.patch.object(app, "APP_PASSWORD", ""):
            self.assertEqual(call(FakeTable(), "GET", "/api/trades", password="")[0], 200)

    def test_errors_are_hidden(self):
        broken = mock.Mock()
        broken.scan.side_effect = RuntimeError("secret detail")
        status, body = call(broken, "GET", "/api/trades")
        self.assertEqual(status, 500)
        self.assertNotIn("secret", body["error"])

    def test_bad_json(self):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/api/trades",
                 "headers": {"x-app-password": "secret-pass"}, "body": "[1,2"}
        with mock.patch.object(app, "TABLE", FakeTable()):
            self.assertEqual(app.handler(event, None)["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
