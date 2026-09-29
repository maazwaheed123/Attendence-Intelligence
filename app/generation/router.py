"""Model router: ordered fallback chain with circuit breaker and output validation.

    ollama-primary (qwen2.5 7b) -> ollama-fallback (qwen2.5 3b) -> template (no LLM)

The router tries providers in order, skipping open circuits and (unless the
tenant allows it) external providers. A provider "fails" on timeout, 429, 5xx,
missing model, or output that is not valid for the requested schema even after
one repair attempt. If every provider fails, ProviderUnavailable is raised and
the caller uses its deterministic fallback (template engine / Tesseract-only OCR).
Every call and every failure is audited with the fallback path.
"""

import json
import logging
import re
from functools import lru_cache

from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.generation.breaker import CircuitBreaker, RedisBackend
from app.generation.providers.base import (
    LLMProvider,
    LLMResult,
    ProviderBadResponse,
    ProviderError,
    ProviderUnavailable,
)
from app.generation.providers.openai_compat import OpenAICompatibleProvider, image_message

log = logging.getLogger(__name__)

TEMPLATE = "template"
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def extract_json(text: str) -> dict:
    """Tolerate code fences / chatter around a single JSON object."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


class LLMRouter:
    def __init__(
        self,
        providers: list[LLMProvider],
        breaker: CircuitBreaker,
        *,
        template_fallback: bool = True,
        timeout_s: float = 90.0,
        max_tokens: int = 800,
    ):
        self.providers = providers
        self.breaker = breaker
        self.template_fallback = template_fallback
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    def complete(
        self,
        messages: list[dict],
        *,
        response_model: type[BaseModel] | None = None,
        allow_external: bool = False,
        purpose: str = "generic",
        audit: dict | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        attempts: list[dict] = []
        for p in self.providers:
            if p.external and not allow_external:
                attempts.append({"provider": p.name, "error": "skipped_external"})
                continue
            if not self.breaker.allow(p.name):
                attempts.append({"provider": p.name, "error": "circuit_open"})
                continue
            try:
                result = self._call(p, messages, response_model, max_tokens or self.max_tokens)
            except ProviderError as exc:
                self.breaker.failure(p.name)
                attempts.append({"provider": p.name, "error": exc.kind, "detail": str(exc)[:200]})
                log.warning("provider %s failed for %s: %s", p.name, purpose, exc)
                continue
            self.breaker.success(p.name)
            result.attempts = attempts
            result.fallback_path = ">".join(
                [a["provider"] for a in attempts if a["error"] not in ("skipped_external",)]
                + [p.name]
            )
            self._audit(audit, purpose, result=result, attempts=attempts)
            return result
        self._audit(audit, purpose, result=None, attempts=attempts)
        raise ProviderUnavailable(attempts, template_allowed=self.template_fallback)

    def complete_vision(self, prompt: str, image: bytes, *, mime="image/png", **kw) -> LLMResult:
        return self.complete(image_message(prompt, image, mime), **kw)

    def _call(self, p: LLMProvider, messages, response_model, max_tokens) -> LLMResult:
        json_mode = response_model is not None
        result = p.chat(
            messages, json_mode=json_mode, timeout=self.timeout_s, max_tokens=max_tokens
        )
        if response_model is None:
            return result
        try:
            result.parsed = response_model.model_validate(extract_json(result.text))
            return result
        except (json.JSONDecodeError, ValidationError, TypeError) as first_error:
            repair = [
                *messages,
                {"role": "assistant", "content": result.text[:2000]},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply was not valid JSON for the required schema "
                        f"({str(first_error)[:300]}). Reply again with ONLY the JSON object."
                    ),
                },
            ]
            retry = p.chat(repair, json_mode=True, timeout=self.timeout_s, max_tokens=max_tokens)
            try:
                retry.parsed = response_model.model_validate(extract_json(retry.text))
                retry.latency_ms += result.latency_ms
                return retry
            except (json.JSONDecodeError, ValidationError, TypeError) as exc:
                raise ProviderBadResponse(f"invalid structured output: {str(exc)[:200]}") from exc

    def _audit(
        self, audit: dict | None, purpose: str, *, result: LLMResult | None, attempts: list[dict]
    ):
        if not audit:
            return
        from app.governance import audit as audit_log

        audit_log.record(
            "llm_call",
            audit["request_id"],
            provider=result.provider if result else None,
            model=result.model if result else None,
            fallback_path=(
                result.fallback_path
                if result
                else ">".join(a["provider"] for a in attempts) + ">" + TEMPLATE
            ),
            outcome="ok" if result else "all_providers_failed",
            error_code=None if result else "PROVIDER_UNAVAILABLE",
            latency_ms=result.latency_ms if result else None,
            details={"purpose": purpose, "attempts": attempts},
            **audit.get("context", {}),
        )

    def health(self, ping: bool = True) -> list[dict]:
        out = []
        for p in self.providers:
            info = {
                "provider": p.name,
                "model": p.model,
                "external": p.external,
                "circuit": self.breaker.state(p.name),
            }
            if ping:
                info.update(p.ping())
            out.append(info)
        return out


def build_provider(name: str) -> LLMProvider:
    s = get_settings()
    if name == "ollama-primary":
        return OpenAICompatibleProvider(name, s.ollama_base_url, s.ollama_primary_model)
    if name == "ollama-fallback":
        return OpenAICompatibleProvider(name, s.ollama_base_url, s.ollama_fallback_model)
    if name == "ollama-vision":
        return OpenAICompatibleProvider(
            name, s.ollama_base_url, s.ollama_vision_model, supports_vision=True
        )
    if name == "mock":
        from app.generation.providers.mock import MockProvider

        return MockProvider()
    raise ValueError(f"unknown provider '{name}'")


def _breaker() -> CircuitBreaker:
    from app.security.ratelimit import get_redis

    s = get_settings()
    return CircuitBreaker(
        RedisBackend(get_redis()), fails=s.breaker_fails, reset_s=s.breaker_reset_s
    )


def _router(chain: list[str], timeout_s: float | None = None) -> LLMRouter:
    s = get_settings()
    return LLMRouter(
        [build_provider(n) for n in chain if n != TEMPLATE],
        _breaker(),
        template_fallback=TEMPLATE in chain,
        timeout_s=timeout_s or s.llm_timeout_s,
        max_tokens=s.llm_max_tokens,
    )


@lru_cache
def get_router() -> LLMRouter:
    return _router(get_settings().llm_chain_list)


@lru_cache
def get_vision_router() -> LLMRouter:
    s = get_settings()
    return _router(s.vision_chain_list, s.vision_timeout_s)


def reset_routers() -> None:
    get_router.cache_clear()
    get_vision_router.cache_clear()
