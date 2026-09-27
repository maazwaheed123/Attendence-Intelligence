"""Field-level encryption for restricted PII (phone, national ID, email)."""

from functools import lru_cache

from cryptography.fernet import Fernet

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().pii_encryption_key.get_secret_value()
    if not key:
        raise RuntimeError("PII_ENCRYPTION_KEY is not configured")
    return Fernet(key.encode())


def encrypt(value: str | None) -> str | None:
    return None if value is None else _fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    return None if token is None else _fernet().decrypt(token.encode()).decode()
