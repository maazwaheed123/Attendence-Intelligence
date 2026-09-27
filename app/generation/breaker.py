"""Per-provider circuit breaker (closed -> open -> half-open -> closed).

State lives in Redis so the API and worker share it and /v1/health/deep can show
it; if Redis itself is down an in-process memory backend is used instead.
"""

import time

import redis


class MemoryBackend:
    def __init__(self):
        self.store: dict[str, tuple[str, float | None]] = {}

    def get(self, key):
        v = self.store.get(key)
        if v is None or (v[1] is not None and v[1] < time.time()):
            return None
        return v[0]

    def set(self, key, value, ttl=None, nx=False):
        if nx and self.get(key) is not None:
            return False
        self.store[key] = (str(value), time.time() + ttl if ttl else None)
        return True

    def incr(self, key, ttl):
        n = int(self.get(key) or 0) + 1
        self.set(key, n, ttl)
        return n

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


class RedisBackend:
    def __init__(self, client: redis.Redis):
        self.r = client

    def get(self, key):
        v = self.r.get(key)
        return v.decode() if isinstance(v, bytes) else v

    def set(self, key, value, ttl=None, nx=False):
        return bool(self.r.set(key, value, ex=int(ttl) if ttl else None, nx=nx))

    def incr(self, key, ttl):
        n = self.r.incr(key)
        self.r.expire(key, int(ttl))
        return n

    def delete(self, *keys):
        self.r.delete(*keys)


class CircuitBreaker:
    def __init__(self, backend, *, fails: int = 3, reset_s: int = 60, fallback=None):
        self.backend = backend
        self.fallback = fallback or MemoryBackend()
        self.fails = fails
        self.reset_s = reset_s

    def _call(self, op, *args, **kw):
        try:
            return getattr(self.backend, op)(*args, **kw)
        except redis.RedisError:
            return getattr(self.fallback, op)(*args, **kw)

    def allow(self, name: str) -> bool:
        """False while open. After the cool-down, exactly one probe call is allowed."""
        open_until = self._call("get", f"cb:{name}:open_until")
        if open_until is None:
            return True
        if float(open_until) > time.time():
            return False
        return bool(self._call("set", f"cb:{name}:probe", 1, ttl=self.reset_s, nx=True))

    def success(self, name: str) -> None:
        self._call("delete", f"cb:{name}:fails", f"cb:{name}:open_until", f"cb:{name}:probe")

    def failure(self, name: str) -> None:
        n = self._call("incr", f"cb:{name}:fails", self.reset_s * 10)
        if n >= self.fails:
            self._call(
                "set", f"cb:{name}:open_until", time.time() + self.reset_s, ttl=self.reset_s * 10
            )
            self._call("delete", f"cb:{name}:fails", f"cb:{name}:probe")

    def state(self, name: str) -> str:
        open_until = self._call("get", f"cb:{name}:open_until")
        if open_until is None:
            return "closed"
        return "open" if float(open_until) > time.time() else "half_open"
