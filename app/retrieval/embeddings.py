"""Text embeddings behind one small interface.

  OllamaEmbedder  nomic-embed-text through Ollama's OpenAI-compatible /v1/embeddings
                  (local, no API key, 768 dims). nomic expects task prefixes:
                  "search_document: " for stored chunks, "search_query: " for queries.
  FakeEmbedder    deterministic feature hashing of words and word pairs; texts that
                  share words get similar vectors, so nearest-neighbour tests are
                  meaningful without any model. Used by the test suite (EMBEDDER=fake).

Only PII-masked text is ever embedded (callers pass text_masked).
"""

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import httpx

from app.config import get_settings

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "


class EmbeddingUnavailable(RuntimeError):
    """The embedding backend could not produce vectors right now (retry later)."""


class Embedder(Protocol):
    name: str
    model: str
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def ping(self) -> dict: ...


def _normalize(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class FakeEmbedder:
    name = "fake"
    model = "hashing-v1"
    _WORD = re.compile(r"[a-z0-9]+")

    def __init__(self, dim: int = 768):
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        words = self._WORD.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
        v = [0.0] * self.dim
        for f in features or ["<empty>"]:
            h = hashlib.blake2b(f.encode(), digest_size=8).digest()
            index = int.from_bytes(h[:4], "big") % self.dim
            v[index] += 1.0 if h[4] & 1 else -1.0
        return _normalize(v)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def ping(self) -> dict:
        return {"reachable": True, "model_available": True}


class OllamaEmbedder:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        *,
        batch: int = 32,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self._base = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.batch = batch
        self.timeout = timeout
        self._transport = transport

    def _client(self, timeout: float) -> httpx.Client:
        return httpx.Client(timeout=timeout, transport=self._transport)

    def _embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            with self._client(self.timeout) as c:
                r = c.post(f"{self._base}/embeddings", json={"model": self.model, "input": inputs})
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(
                f"embedding backend unreachable: {exc.__class__.__name__}"
            ) from exc
        if r.status_code == 404:
            raise EmbeddingUnavailable(f"embedding model '{self.model}' not found")
        if r.status_code >= 400:
            raise EmbeddingUnavailable(f"embedding request failed with {r.status_code}")
        try:
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [d["embedding"] for d in data]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingUnavailable("malformed embedding response") from exc
        if len(vectors) != len(inputs) or any(len(v) != self.dim for v in vectors):
            raise EmbeddingUnavailable(f"expected {len(inputs)} vectors of {self.dim} dims")
        return [_normalize(v) for v in vectors]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out += self._embed([DOC_PREFIX + t for t in texts[i : i + self.batch]])
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._embed([QUERY_PREFIX + text])[0]

    def ping(self) -> dict:
        try:
            with self._client(2.0) as c:
                r = c.get(f"{self._base}/models")
            models = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return {"reachable": False, "model_available": False}
        available = any(m == self.model or m.split(":")[0] == self.model for m in models)
        return {"reachable": r.status_code == 200, "model_available": available}


@lru_cache
def get_embedder() -> Embedder:
    s = get_settings()
    if s.embedder == "fake":
        return FakeEmbedder(s.embed_dim)
    return OllamaEmbedder(
        s.ollama_base_url,
        s.ollama_embed_model,
        s.embed_dim,
        batch=s.embed_batch,
        timeout=s.embed_timeout_s,
    )
