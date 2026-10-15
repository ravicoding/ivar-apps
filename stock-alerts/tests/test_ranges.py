"""Range alert tests. Run: python3 -m unittest discover -s tests"""
import json
import re
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["APP_PASSWORD"] = "secret-pass"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import app  # noqa: E402


class ConditionFailed(Exception):
    pass


class FakeTable:
    """Just enough of a DynamoDB Table for app.py."""

    def __init__(self, items):
        self.items = {i["id"]: dict(i) for i in items}
        self.meta = mock.Mock()
        self.meta.client.exceptions.ConditionalCheckFailedException = ConditionFailed
        self.zone_conflict = False

    def scan(self, **_):
        return {"Items": [app.to_ddb(i) for i in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": app.to_ddb(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["id"]] = app.from_ddb(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None,
                    ExpressionAttributeNames=None, ConditionExpression=None):
        if ConditionExpression is not None and self.zone_conflict:
            raise ConditionFailed()
        # Like DynamoDB: every value placeholder must be used somewhere in the request.
        # (Placeholders made by boto3 condition objects are added later by boto3 itself.)
        for key in ExpressionAttributeValues or {}:
            if not re.search(re.escape(key) + r"\b", UpdateExpression):
                raise ValueError(f"Value provided in ExpressionAttributeValues unused in expressions: keys: {{{key}}}")
        vals = app.from_ddb(ExpressionAttributeValues or {})
        names = ExpressionAttributeNames or {}
        item = self.items.setdefault(Key["id"], {"id": Key["id"]})
        for part in re.split(r", (?=[#\w]+ = )", UpdateExpression[4:]):
            k, v = part.split(" = ", 1)
            k = names.get(k, k)
            if v.startswith("if_not_exists"):
                item[k] = item.get(k, 0) + 1
            else:
                item[k] = vals[v]


def range_alert(**kw):
    a = {"id": "r1", "kind": "range", "ticker": "AAPL", "low": 100.0, "high": 120.0,
         "zone": "inside", "status": "watching", "created_at": "2026-10-14T00:00:00+00:00"}
    a.update(kw)
    return a


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.sms = mock.patch.object(app, "send_sms", return_value=None).start()
        self.push = mock.patch.object(app, "push", return_value=None).start()
        mock.patch.object(app, "PHONE_NUMBER", "+15550000000").start()
        self.addCleanup(mock.patch.stopall)

    def run_at(self, table, price):
        with mock.patch.object(app, "TABLE", table), \
             mock.patch.object(app, "get_prices", return_value=({"AAPL": price}, [])):
            return app.run_check()

    def test_zone_of(self):
        self.assertEqual(app.zone_of(99, 100, 120), "below")
        self.assertEqual(app.zone_of(100, 100, 120), "below")
        self.assertEqual(app.zone_of(110, 100, 120), "inside")
        self.assertEqual(app.zone_of(120, 100, 120), "above")
        self.assertEqual(app.zone_of(500, 100, None), "inside")
        self.assertEqual(app.zone_of(5, None, 120), "inside")

    def test_alerts_once_per_crossing(self):
        t = FakeTable([range_alert()])
        self.assertEqual(self.run_at(t, 110)["hits"], 0)
        self.assertEqual(self.run_at(t, 95)["hits"], 1)        # fell below low
        self.assertIn("below your low of $100.00", self.sms.call_args[0][0])
        self.assertEqual(self.run_at(t, 94)["hits"], 0)        # still below: no repeat
        self.assertEqual(self.run_at(t, 105)["hits"], 0)       # back inside: re-armed
        self.assertEqual(self.run_at(t, 125)["hits"], 1)       # rose above high
        self.assertIn("above your high of $120.00", self.sms.call_args[0][0])
        self.assertEqual(self.sms.call_count, 2)
        self.assertEqual(t.items["r1"]["alerts_sent"], 2)
        self.assertEqual(t.items["r1"]["zone"], "above")
        self.assertEqual(t.items["r1"]["status"], "watching")

    def test_jump_from_below_to_above_alerts(self):
        t = FakeTable([range_alert(zone="below")])
        self.assertEqual(self.run_at(t, 130)["hits"], 1)

    def test_concurrent_run_does_not_double_send(self):
        t = FakeTable([range_alert()])
        t.zone_conflict = True
        self.assertEqual(self.run_at(t, 95)["hits"], 0)
        self.sms.assert_not_called()

    def test_sends_push_when_topic_set(self):
        t = FakeTable([range_alert(), {"id": app.SETTINGS_ID, "ntfy_topic": "stocks-abc"}])
        self.run_at(t, 95)
        self.push.assert_called_once()
        self.assertEqual(self.push.call_args[0][0], "stocks-abc")

    def test_one_failing_alert_does_not_stop_others(self):
        t = FakeTable([range_alert(id="bad"), range_alert(id="good", ticker="MSFT")])
        real = app.check_range
        def flaky(a, *args):
            if a["id"] == "bad":
                raise RuntimeError("boom")
            return real(a, *args)
        with mock.patch.object(app, "check_range", side_effect=flaky), mock.patch.object(app, "TABLE", t), \
             mock.patch.object(app, "get_prices", return_value=({"AAPL": 95, "MSFT": 95}, [])):
            r = app.run_check()
        self.assertEqual(r["hits"], 1)
        self.assertIn("AAPL: boom", r["errors"])
        self.assertEqual(t.items[app.SETTINGS_ID]["last_error"], "AAPL: boom")

    def test_target_alerts_still_work(self):
        t = FakeTable([{"id": "t1", "ticker": "AAPL", "target": 120.0, "direction": "above",
                        "status": "watching", "created_at": "x"}])
        self.assertEqual(self.run_at(t, 121)["hits"], 1)
        self.assertEqual(t.items["t1"]["status"], "triggered")


class CreateRangeTests(unittest.TestCase):
    def post(self, body, price=110.0):
        t = FakeTable([])
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/api/alerts",
                 "headers": {"x-app-password": "secret-pass"}, "body": json.dumps(body)}
        with mock.patch.object(app, "TABLE", t), mock.patch.object(app, "get_price", return_value=price):
            r = app.handle_http(event)
        return r["statusCode"], json.loads(r["body"]), t

    def test_creates_range(self):
        status, body, t = self.post({"ticker": "aapl", "low": "100", "high": "120.5"})
        self.assertEqual(status, 201)
        self.assertEqual((body["ticker"], body["low"], body["high"], body["zone"]), ("AAPL", 100.0, 120.5, "inside"))
        self.assertEqual(body["now_outside"], "inside")
        self.assertEqual(len(t.items), 1)

    def test_one_sided_range(self):
        status, body, t = self.post({"ticker": "AAPL", "low": "", "high": 130})
        self.assertEqual(status, 201)
        self.assertNotIn("low", next(iter(t.items.values())))

    def test_reports_already_outside(self):
        _, body, _ = self.post({"ticker": "AAPL", "low": 100, "high": 105}, price=110)
        self.assertEqual(body["now_outside"], "above")

    def test_validation(self):
        self.assertIn("low price must be below", self.post({"ticker": "AAPL", "low": 120, "high": 100})[1]["error"])
        self.assertIn("low price, a high price, or both", self.post({"ticker": "AAPL", "low": "", "high": ""})[1]["error"])
        self.assertIn("above zero", self.post({"ticker": "AAPL", "low": -1})[1]["error"])
        self.assertIn("number", self.post({"ticker": "AAPL", "high": "abc"})[1]["error"])
        self.assertIn("ticker", self.post({"ticker": "", "high": 5})[1]["error"])

    def test_requires_password(self):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/api/alerts", "headers": {}, "body": "{}"}
        self.assertEqual(app.handle_http(event)["statusCode"], 401)


if __name__ == "__main__":
    unittest.main()
