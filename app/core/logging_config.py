"""
Structured (JSON) logging setup.

Plain text logs are fine on a laptop; in production, JSON lines are what
lets CloudWatch/Datadog/ELK actually index and alert on fields like
job_id/status/error_code instead of grepping strings (Q: "how would you
expose this as an API?" -> "proper logging, and tracing implementation").
"""
from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
