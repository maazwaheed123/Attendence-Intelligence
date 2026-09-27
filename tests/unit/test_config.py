import pytest

from app.config import Settings

pytestmark = pytest.mark.unit


def test_defaults_are_local_only():
    s = Settings(_env_file=None)
    assert s.llm_chain_list == ["ollama-primary", "ollama-fallback", "template"]
    assert "11434" in s.ollama_base_url
    assert 0 < s.conf_low < s.conf_high <= 1


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_CHAIN", "mock, template")
    monkeypatch.setenv("INGEST_SYNC", "true")
    s = Settings(_env_file=None)
    assert s.llm_chain_list == ["mock", "template"]
    assert s.ingest_sync is True


def test_secrets_never_rendered(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "super-secret-value-123")
    monkeypatch.setenv("DATABASE_URL_OWNER", "postgresql://u:db-pass-xyz@h/d")
    s = Settings(_env_file=None)
    rendered = repr(s) + str(s) + s.model_dump_json()
    assert "super-secret-value-123" not in rendered
    assert "db-pass-xyz" not in rendered
    assert s.jwt_secret.get_secret_value() == "super-secret-value-123"


def test_invalid_threshold_rejected(monkeypatch):
    monkeypatch.setenv("OCR_REVIEW_THRESHOLD", "1.5")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
