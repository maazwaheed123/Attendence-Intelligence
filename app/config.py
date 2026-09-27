"""Central configuration. Every tunable comes from the environment (.env in dev).

Secrets are SecretStr so they never appear in repr(), logs or error output.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- service ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_name: str = "attendance-intelligence"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # --- auth ---
    jwt_secret: SecretStr = SecretStr("change-me")
    jwt_alg: str = "HS256"
    jwt_ttl_min: int = 60
    allowed_modules: str = "attendance"
    dev_personas_file: str = "scripts/seed_spec.yaml"  # dev/test only: /v1/auth/dev-token

    # --- stores ---
    database_url_owner: SecretStr = SecretStr(
        "postgresql+psycopg://postgres:postgres@postgres:5432/attendance"
    )
    database_url_app: SecretStr = SecretStr(
        "postgresql+psycopg://app_rw:app_rw@postgres:5432/attendance"
    )
    database_url_reader: SecretStr = SecretStr(
        "postgresql+psycopg://rag_reader:rag_reader@postgres:5432/attendance"
    )
    redis_url: str = "redis://redis:6379/0"
    pii_encryption_key: SecretStr = SecretStr("")

    # --- ingestion ---
    ingest_sync: bool = False
    max_upload_mb: int = 20
    upload_dir: str = "data/uploads"

    # --- LLM (local Ollama only, no API keys) ---
    llm_chain: str = "ollama-primary,ollama-fallback,template"
    ollama_base_url: str = "http://host.docker.internal:11434/v1"
    ollama_primary_model: str = "qwen2.5:7b-instruct"
    ollama_fallback_model: str = "qwen2.5:3b-instruct"
    ollama_vision_model: str = "qwen2.5vl:3b"
    vision_chain: str = "ollama-vision"
    llm_timeout_s: float = 90.0
    llm_max_tokens: int = 800
    breaker_fails: int = 3
    breaker_reset_s: int = 60

    # --- retrieval / governance thresholds ---
    embed_model: str = "BAAI/bge-small-en-v1.5"
    rerank_model: str = "BAAI/bge-reranker-base"
    ocr_review_threshold: float = Field(0.75, ge=0, le=1)
    feedback_match_threshold: float = Field(0.85, ge=0, le=1)
    conf_high: float = Field(0.85, ge=0, le=1)
    conf_low: float = Field(0.60, ge=0, le=1)
    cache_ttl_s: int = 600
    rate_limit_per_min: int = 60

    @property
    def allowed_modules_list(self) -> list[str]:
        return [m.strip() for m in self.allowed_modules.split(",") if m.strip()]

    @property
    def dev_tokens_enabled(self) -> bool:
        return self.app_env in ("dev", "test")

    @property
    def llm_chain_list(self) -> list[str]:
        return [p.strip() for p in self.llm_chain.split(",") if p.strip()]

    @property
    def vision_chain_list(self) -> list[str]:
        return [p.strip() for p in self.vision_chain.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
