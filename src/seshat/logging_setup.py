"""Структурное логирование в JSON с вычисткой секретов.

Логи уходят в stdout и подбираются драйвером json-file Docker'а.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# Telegram-токен и ключ NVIDIA не должны попасть в лог даже случайно —
# например, из текста исключения httpx с полным URL.
#
# Границу слова (\b) здесь использовать нельзя: в URL токен идёт как
# `/bot123456789:AA...`, а между `t` и `1` границы нет, потому что оба символа
# словесные. Поэтому отрицательный ретроспективный поиск по цифре — он отсекает
# только середину более длинного числа.
_SECRET_PATTERNS = [
    re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"(?<![\w-])nvapi-[\w-]{20,}"),  # NVIDIA API key
]


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": _redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = _redact(self.formatException(record.exc_info))
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # aiogram по умолчанию слишком разговорчив на INFO
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
