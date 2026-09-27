"""JWT issue/verify. HS256 for the MVP (RS256 + key rotation in production).

Verification pins the algorithm (so "alg: none" and algorithm confusion are
rejected), and requires exp/iat/jti/sub plus the isolation claims.
"""

import time
import uuid

import jwt

from app.config import get_settings

ISSUER = "attendance-intelligence"
AUDIENCE = "attendance-api"
REQUIRED_CLAIMS = ["exp", "iat", "jti", "sub", "iss", "aud"]


class TokenError(Exception):
    """Token missing, malformed, expired or with invalid claims."""


def issue_token(claims: dict, ttl_min: int | None = None) -> str:
    s = get_settings()
    now = int(time.time())
    payload = {
        **claims,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 60 * (ttl_min or s.jwt_ttl_min),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=s.jwt_alg)


def verify_token(token: str) -> dict:
    s = get_settings()
    try:
        return jwt.decode(
            token,
            s.jwt_secret.get_secret_value(),
            algorithms=[s.jwt_alg],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.PyJWTError as exc:
        raise TokenError("invalid token") from exc
