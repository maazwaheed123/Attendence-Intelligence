"""Token helpers for tests, including deliberately malicious tokens."""

import base64
import json
import time
import uuid

import jwt

from app.api.routes_auth import persona_claims
from app.config import get_settings
from app.security.jwt import AUDIENCE, ISSUER, issue_token


def token_for(persona: str, ttl_min: int = 5, **overrides) -> str:
    claims = {**persona_claims(persona), **overrides}
    claims = {k: v for k, v in claims.items() if v is not None}
    return issue_token(claims, ttl_min)


def forge(
    persona: str, *, secret: str | None = None, alg: str = "HS256", drop=(), **overrides
) -> str:
    """Sign arbitrary claims (wrong secret, missing claims, expired, ...)."""
    now = int(time.time())
    claims = {
        **persona_claims(persona),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": uuid.uuid4().hex,
        **overrides,
    }
    for k in drop:
        claims.pop(k, None)
    key = secret if secret is not None else get_settings().jwt_secret.get_secret_value()
    return jwt.encode(claims, key, algorithm=alg)


def unsigned(persona: str) -> str:
    """An 'alg: none' token (classic JWT bypass attempt)."""

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    now = int(time.time())
    claims = {
        **persona_claims(persona),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
        "jti": "x",
    }
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."
