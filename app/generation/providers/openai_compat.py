"""One adapter for every OpenAI-compatible Chat Completions endpoint.

Used for local Ollama (http://host.docker.internal:11434/v1). A cloud provider
(NVIDIA NIM, Groq, OpenRouter, ...) would be the same class with a different
base_url/model/api_key and external=True - config only, no code change.
"""

import base64
import time

import httpx

from app.generation.providers.base import (
    LLMResult,
    ProviderBadResponse,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
)


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        external: bool = False,
        supports_vision: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.name = name
        self.model = model
        self.external = external
        self.supports_vision = supports_vision
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, headers=self._headers, transport=self._transport)

    def chat(self, messages, *, json_mode, timeout, max_tokens) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,  # deterministic as far as the model allows
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        start = time.perf_counter()
        try:
            with self._client(timeout) as c:
                r = c.post(f"{self._base}/chat/completions", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"timeout after {timeout}s") from exc
        except httpx.TransportError as exc:
            raise ProviderNotConfigured(f"unreachable: {exc.__class__.__name__}") from exc
        if r.status_code == 429:
            raise ProviderRateLimited("429 rate limited")
        if r.status_code == 404:
            raise ProviderNotConfigured(f"model '{self.model}' not found")
        if r.status_code >= 500:
            raise ProviderServerError(f"{r.status_code} server error")
        if r.status_code >= 400:
            raise ProviderBadResponse(f"{r.status_code} rejected request")
        try:
            body = r.json()
            text = body["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderBadResponse("malformed completion payload") from exc
        return LLMResult(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=int((time.perf_counter() - start) * 1000),
            usage=body.get("usage") or {},
        )

    def ping(self, timeout: float = 2.0) -> dict:
        try:
            with self._client(timeout) as c:
                r = c.get(f"{self._base}/models")
            models = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return {"reachable": False, "model_available": False}
        return {"reachable": r.status_code == 200, "model_available": self.model in models}


def image_message(prompt: str, image: bytes, mime: str = "image/png") -> list[dict]:
    """OpenAI-style multimodal user message (supported by Ollama vision models)."""
    data = base64.b64encode(image).decode()
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
            ],
        }
    ]
