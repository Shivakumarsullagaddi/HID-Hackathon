"""Snapshot signing: HMAC-SHA256 over canonical JSON.

Stand-in for production, where central signs with an asymmetric key held in
a KMS and kiosks hold only the public key (so a stolen kiosk cannot forge a
snapshot). With HMAC every kiosk holds the signing secret.
"""
import base64
import hashlib
import hmac

from .outbox import canonical_json

ALG = "HMAC-SHA256"


def sign_body(body, key):
    return hmac.new(key.encode(), canonical_json(body).encode(), hashlib.sha256).hexdigest()


def signature_ok(body, signature, key):
    if not isinstance(signature, str):
        return False
    return hmac.compare_digest(sign_body(body, key), signature)


def b64_embedding(blob):
    return base64.b64encode(blob).decode("ascii")


def unb64_embedding(text):
    return base64.b64decode(text.encode("ascii"), validate=True)
