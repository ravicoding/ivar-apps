"""Web push tests: decrypt exactly as a phone's browser would, check the VAPID JWT."""
import base64
import hashlib
import hmac
import json
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

import webpush  # noqa: E402
import test_ranges  # noqa: E402  (sets env and imports app)
app = test_ranges.app


def hkdf(salt, ikm, info, n):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]


class Browser:
    """The receiving side of RFC 8291, written independently of webpush.encrypt."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = webpush.raw_public_key(self.key.public_key())
        self.auth = os.urandom(16)

    def subscription(self, endpoint="https://web.push.apple.com/abc"):
        return {"endpoint": endpoint, "keys": {"p256dh": webpush.b64url(self.public), "auth": webpush.b64url(self.auth)}}

    def decrypt(self, body):
        salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
        as_public = body[21:21 + idlen]
        self_test = (rs, idlen)
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.public + as_public, 32)
        cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
        assert plain.endswith(b"\x02"), "missing last-record delimiter"
        return plain[:-1], self_test


class WebPushTests(unittest.TestCase):
    def test_round_trip(self):
        b = Browser()
        sub = b.subscription()
        body = webpush.encrypt(b'{"title":"SPY"}', sub["keys"]["p256dh"], sub["keys"]["auth"])
        plain, (rs, idlen) = b.decrypt(body)
        self.assertEqual(plain, b'{"title":"SPY"}')
        self.assertEqual((rs, idlen), (4096, 65))

    def test_rejects_bad_keys(self):
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x", webpush.b64url(b"short"), webpush.b64url(os.urandom(16)))

    def test_vapid_jwt_is_valid_es256(self):
        priv, pub = webpush.new_vapid_key()
        key = webpush.load_private_key(priv)
        header = webpush.vapid_header("https://fcm.googleapis.com/fcm/send/xyz", key, "mailto:a@b.c")
        t = header.split("t=")[1].split(",")[0]
        k = header.split("k=")[1]
        self.assertEqual(k, pub)
        h, c, sig = t.split(".")
        claims = json.loads(webpush.b64url_decode(c))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        self.assertEqual(claims["sub"], "mailto:a@b.c")
        raw = webpush.b64url_decode(sig)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        pubkey = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64url_decode(pub))
        pubkey.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if invalid

    def test_send_posts_encrypted_body(self):
        b = Browser()
        key = webpush.load_private_key(webpush.new_vapid_key()[0])
        captured = {}

        class Resp:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake_urlopen(req, timeout):
            captured["req"] = req
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            webpush.send(b.subscription(), {"title": "SPY fell"}, key, "mailto:a@b.c")
        req = captured["req"]
        self.assertEqual(req.get_header("Content-encoding"), "aes128gcm")
        self.assertEqual(req.get_header("Urgency"), "high")
        self.assertEqual(json.loads(b.decrypt(req.data)[0]), {"title": "SPY fell"})

    def test_gone_subscription(self):
        import urllib.error
        b = Browser()
        key = webpush.load_private_key(webpush.new_vapid_key()[0])
        err = urllib.error.HTTPError("u", 410, "Gone", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(webpush.Gone):
                webpush.send(b.subscription(), {}, key, "mailto:a@b.c")


class AppPushTests(unittest.TestCase):
    def setUp(self):
        self.key = webpush.load_private_key(webpush.new_vapid_key()[0])
        mock.patch.object(app, "vapid_key", return_value=self.key).start()
        mock.patch.object(app, "PHONE_NUMBER", "").start()
        self.addCleanup(mock.patch.stopall)

    def http(self, table, method, path, body=None):
        event = {"requestContext": {"http": {"method": method}}, "rawPath": path,
                 "headers": {"x-app-password": "secret-pass"}, "body": json.dumps(body) if body is not None else None}
        with mock.patch.object(app, "TABLE", table):
            r = app.handle_http(event)
        return r["statusCode"], json.loads(r["body"])

    def test_subscribe_list_and_unsubscribe(self):
        t = test_ranges.FakeTable([])
        sub = Browser().subscription()
        status, body = self.http(t, "POST", "/api/push/subscribe", {"subscription": sub, "label": "iPhone"})
        self.assertEqual(status, 200)
        status, state = self.http(t, "GET", "/api/state")
        self.assertEqual([d["label"] for d in state["devices"]], ["iPhone"])
        self.assertEqual(state["alerts"], [])  # devices never show up as alerts
        self.http(t, "POST", "/api/push/unsubscribe", {"endpoint": sub["endpoint"]})
        self.assertEqual(self.http(t, "GET", "/api/state")[1]["devices"], [])

    def test_rejects_bad_subscription(self):
        t = test_ranges.FakeTable([])
        self.assertEqual(self.http(t, "POST", "/api/push/subscribe", {"subscription": {"endpoint": "http://x"}})[0], 400)

    def test_public_key_route(self):
        status, body = self.http(test_ranges.FakeTable([]), "GET", "/api/push/key")
        self.assertEqual(body["publicKey"], webpush.b64url(webpush.raw_public_key(self.key.public_key())))

    def test_alert_pushes_to_phone_and_drops_dead_ones(self):
        good, dead = Browser(), Browser()
        t = test_ranges.FakeTable([
            test_ranges.range_alert(),
            {"id": app.push_id("https://push.example/good"), "kind": "push", "label": "Phone",
             **good.subscription("https://push.example/good")},
            {"id": app.push_id("https://push.example/dead"), "kind": "push", "label": "Old",
             **dead.subscription("https://push.example/dead")},
        ])
        bodies = []

        def fake_send(sub, data, key, subject):
            if sub["endpoint"].endswith("dead"):
                raise webpush.Gone()
            bodies.append(data)

        with mock.patch.object(app, "TABLE", t), mock.patch("webpush.send", fake_send), \
             mock.patch.object(app, "get_prices", return_value=({"AAPL": 95.0}, [])):
            r = app.run_check()
        self.assertEqual(r["hits"], 1)
        self.assertEqual(r["errors"], [])
        self.assertEqual(bodies[0]["title"], "AAPL fell to $95.00")
        self.assertNotIn(app.push_id("https://push.example/dead"), t.items)


if __name__ == "__main__":
    unittest.main()
