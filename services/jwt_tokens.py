"""JSON Web Tokens HS256 (RFC 7519) using only the standard library.

Only the HS256 algorithm is accepted: a token whose header says "none" or any
other algorithm is rejected before the signature is checked.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

HEADER = {"alg": "HS256", "typ": "JWT"}
ACCESS_TTL_SECONDS = 8 * 3600     # absolute lifetime; the 30-minute idle limit is checked in the database


class TokenError(Exception):
    pass


def b64encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64decode(text):
    if not isinstance(text, str) or not text or len(text) % 4 == 1:
        raise TokenError("Malformed token.")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise TokenError("Malformed token.") from None


def compact(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


class TokenSigner:
    def __init__(self, secret=None, issuer="climate-portal", ttl=ACCESS_TTL_SECONDS):
        secret = secret if secret is not None else os.environ.get("CLIMA_JWT_SECRET") or secrets.token_bytes(32)
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(self.secret) < 32:
            raise ValueError("The JWT secret must contain at least 32 bytes.")
        self.issuer, self.ttl = issuer, int(ttl)
        self.header = b64encode(compact(HEADER))

    def signature(self, signing_input):
        return hmac.new(self.secret, signing_input.encode("ascii"), hashlib.sha256).digest()

    def issue(self, claims):
        now = int(time.time())
        body = {"iss": self.issuer, "iat": now, "exp": now + self.ttl, "jti": secrets.token_urlsafe(16), **claims}
        signing_input = self.header + "." + b64encode(compact(body))
        return signing_input + "." + b64encode(self.signature(signing_input))

    def verify(self, token, check_expiry=True):
        if not isinstance(token, str) or not 20 <= len(token) <= 4096:
            raise TokenError("Missing or malformed token.")
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenError("Malformed token.")
        try:
            header = json.loads(b64decode(parts[0]))
        except ValueError:
            raise TokenError("Malformed token.") from None
        if header != HEADER:
            raise TokenError("Unsupported token algorithm.")
        if not hmac.compare_digest(b64decode(parts[2]), self.signature(parts[0] + "." + parts[1])):
            raise TokenError("Invalid token signature.")
        try:
            claims = json.loads(b64decode(parts[1]))
        except ValueError:
            raise TokenError("Malformed token.") from None
        now = time.time()
        if not isinstance(claims, dict) or claims.get("iss") != self.issuer:
            raise TokenError("Invalid token issuer.")
        if not isinstance(claims.get("exp"), int) or not isinstance(claims.get("iat"), int):
            raise TokenError("Invalid token dates.")
        if check_expiry and claims["exp"] <= now:
            raise TokenError("Token expired.")
        if claims["iat"] > now + 60:
            raise TokenError("Token issued in the future.")
        return claims
