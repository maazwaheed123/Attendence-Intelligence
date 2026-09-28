"""Structured logging: one JSON object per line, request id on every line.

The request id comes from the middleware's contextvar, so a log line written
anywhere while serving a request (router, orchestrator, cache) can be joined to
its audit events. Lines outside a request carry request_id "-".
"""

import datetime as dt
import json
import logging

from app.api.middleware import request_id_var

_STD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        # structured extras: log.info("x", extra={"cache": "hit"})
        out.update({k: v for k, v in vars(record).items() if k not in _STD})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers = [h for h in root.handlers if type(h) is not logging.StreamHandler]
    root.addHandler(handler)
    root.setLevel(level)
