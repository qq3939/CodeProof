"""结构化 JSON 日志。

为什么用 contextvars：一个 request / 会话 / 任务里的所有日志自动带上同一个 id，
排查问题时按 id 过滤即可，不需要每次 log 都手动传参。
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, var in (("request_id", request_id_var), ("session_id", session_id_var), ("job_id", job_id_var)):
            val = var.get()
            if val:
                data[key] = val
        extra = getattr(record, "extra_data", None)
        if isinstance(extra, dict):
            data.update(extra)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # 第三方库太吵，调高级别
    for noisy in ("httpx", "httpcore", "jieba", "asyncio"):
        logging.getLogger(noisy).setLevel("WARNING")


def log_extra(**kwargs: object) -> dict[str, object]:
    """用法：logger.info("msg", extra=log_extra(k=v))"""
    return {"extra_data": kwargs}
