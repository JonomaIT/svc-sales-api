"""Unit tests: the rules of the API, with in-memory doubles instead of AWS. Run in the image build."""

import decimal
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
import app  # noqa: E402


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 (boto3 naming)
        assert Item["id"] not in self.items
        self.items[Item["id"]] = dict(Item)

    def get_item(self, Key):  # noqa: N803
        item = self.items.get(Key["id"])
        return {"Item": item} if item else {}

    def delete_item(self, Key, ReturnValues=None):  # noqa: N803
        old = self.items.pop(Key["id"], None)
        return {"Attributes": old} if old else {}


class FakeSNS:
    def __init__(self, fail=False):
        self.fail, self.published = fail, []

    def publish(self, **kwargs):
        if self.fail:
            raise RuntimeError("SNS unavailable")
        self.published.append(kwargs)


class FakeSSM:
    def __init__(self, value="ACTIVE", fail=False):
        self.value, self.fail, self.calls = value, fail, 0

    def get_parameter(self, Name):  # noqa: N803
        self.calls += 1
        if self.fail:
            raise RuntimeError("SSM unavailable")
        return {"Parameter": {"Value": self.value}}


class ParseSale(unittest.TestCase):
    def test_valid_sale(self):
        self.assertEqual(app.parse_sale(b'{"product": " coffee ", "amount": 10.555}'), ("coffee", decimal.Decimal("10.56")))

    def test_invalid_sales(self):
        for body in (b"not json", b"[]", b'{"amount": 1}', b'{"product": "", "amount": 1}', b'{"product": "x", "amount": 0}',
                     b'{"product": "x", "amount": true}', b'{"product": "x", "amount": "10"}', json.dumps({"product": "x" * 101, "amount": 1}).encode()):
            with self.subTest(body=body), self.assertRaises(app.BadRequest):
                app.parse_sale(body)


class CreateSale(unittest.TestCase):
    def test_sale_is_stored_pending_and_published(self):
        table, sns = FakeTable(), FakeSNS()
        sale = app.Sales(table, sns, "arn:aws:sns:us-east-1:111111111111:topic").create("coffee", decimal.Decimal("10"))
        self.assertEqual(table.items[sale["id"]]["status"], "PENDING")
        event = json.loads(sns.published[0]["Message"])
        self.assertEqual((event["id"], event["amount"]), (sale["id"], "10.00"), "money as text, never float")
        self.assertEqual(sns.published[0]["TopicArn"], "arn:aws:sns:us-east-1:111111111111:topic")

    def test_no_event_no_sale(self):
        table = FakeTable()
        with self.assertRaises(RuntimeError):
            app.Sales(table, FakeSNS(fail=True), "arn").create("coffee", decimal.Decimal("10"))
        self.assertEqual(table.items, {}, "a sale nobody will process must not stay stored")


class RegionStateCache(unittest.TestCase):
    def test_cached(self):
        ssm = FakeSSM("PASSIVE")
        state = app.RegionState(ssm, "/jonomait/production/platform/region-state")
        self.assertEqual((state.get(), state.get()), ("PASSIVE", "PASSIVE"))
        self.assertEqual(ssm.calls, 1)

    def test_unknown_when_unavailable(self):
        self.assertEqual(app.RegionState(FakeSSM(fail=True), "/x").get(), "UNKNOWN")
        self.assertEqual(app.RegionState(FakeSSM(), None).get(), "UNKNOWN")


class Http(unittest.TestCase):
    """The real HTTP handler on a random port."""

    @classmethod
    def setUpClass(cls):
        app.SALES = app.Sales(FakeTable(), FakeSNS(), "arn")
        app.STATE = app.RegionState(FakeSSM("ACTIVE"), "/x")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw and resp.headers["Content-Type"] == "application/json" else raw
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_lifecycle(self):
        status, sale = self.call("POST", "/sales", {"product": "coffee", "amount": 12.5})
        self.assertEqual((status, sale["status"], sale["amount"]), (201, "PENDING", "12.50"))
        self.assertEqual(self.call("GET", f"/sales/{sale['id']}")[1]["product"], "coffee")
        self.assertEqual(self.call("DELETE", f"/sales/{sale['id']}")[0], 204)
        self.assertEqual(self.call("GET", f"/sales/{sale['id']}")[0], 404)

    def test_bad_request(self):
        status, body = self.call("POST", "/sales", {"product": "coffee"})
        self.assertEqual(status, 400)
        self.assertIn("amount", body["error"])

    def preflight(self, origin):
        req = urllib.request.Request(self.base + "/sales", method="OPTIONS", headers={"Origin": origin})
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.headers

    def test_cors_only_for_allowed_sites(self):
        app.ALLOWED_ORIGINS = {"https://vendas.example.com"}
        status, headers = self.preflight("https://vendas.example.com")
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (204, "https://vendas.example.com"))
        self.assertIn("X-Region", headers["Access-Control-Expose-Headers"])
        self.assertIsNone(self.preflight("https://evil.example.com")[1]["Access-Control-Allow-Origin"])

    def test_health_and_info(self):
        self.assertEqual(self.call("GET", "/health"), (200, b"ok\n"))
        self.assertEqual(self.call("GET", "/info")[1]["region_state"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
