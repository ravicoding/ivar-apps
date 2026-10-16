"""
Web Push sender (no pywebpush): VAPID auth (RFC 8292) and aes128gcm payload
encryption (RFC 8291 / RFC 8188). Works with Apple, Google and Mozilla push services.
Needs only the `cryptography` package.
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text):
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def raw_public_key(public_key):
    """65-byte uncompressed P-256 point, the form browsers and push services use."""
    return public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def load_private_key(text):
    """VAPID private key from base64url of the raw 32-byte secret."""
    return ec.derive_private_key(int.from_bytes(b64url_decode(text), "big"), ec.SECP256R1())


def new_vapid_key():
    """Returns (private, public) as base64url strings, for one-time setup."""
    key = ec.generate_private_key(ec.SECP256R1())
    secret = key.private_numbers().private_value.to_bytes(32, "big")
    return b64url(secret), b64url(raw_public_key(key.public_key()))


def _hkdf_one_block(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload, p256dh, auth):
    """Encrypt bytes for one subscription. Returns the aes128gcm request body."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    if len(ua_public) != 65 or len(auth_secret) < 16:
        raise ValueError("subscription keys are not valid")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)

    as_private = ec.generate_private_key(ec.SECP256R1())
    as_public = raw_public_key(as_private.public_key())
    shared = as_private.exchange(ec.ECDH(), ua_key)

    ikm = _hkdf_one_block(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf_one_block(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_one_block(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    record = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02: last (and only) record
    header = salt + struct.pack("!IB", 4096, len(as_public)) + as_public
    return header + record


def vapid_header(endpoint, private_key, subject):
    parts = urllib.parse.urlsplit(endpoint)
    claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}
    signing_input = (b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
                     + "." + b64url(json.dumps(claims, separators=(",", ":")).encode()))
    r, s = decode_dss_signature(private_key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={token}, k={b64url(raw_public_key(private_key.public_key()))}"


class Gone(Exception):
    """The push service says this subscription no longer exists (phone unsubscribed or app removed)."""


def send(subscription, data, private_key, subject, ttl=3600):
    """Send one notification. Raises Gone for dead subscriptions, other exceptions for failures."""
    endpoint = subscription["endpoint"]
    if not endpoint.startswith("https://"):
        raise ValueError("push endpoint must be https")
    keys = subscription.get("keys") or {}
    body = encrypt(json.dumps(data).encode("utf-8"), keys.get("p256dh", ""), keys.get("auth", ""))
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": vapid_header(endpoint, private_key, subject),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": "high",
    })
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise Gone() from e
        detail = e.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"push service returned {e.code}: {detail}") from e
