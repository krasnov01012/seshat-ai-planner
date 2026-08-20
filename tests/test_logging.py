"""Логи не должны содержать секретов ни при каких обстоятельствах."""

from __future__ import annotations

import json
import logging

from seshat.logging_setup import JsonFormatter

TOKEN = "123456789:" + "AAHrandomlookingtokenvaluewith30plus"
NVKEY = "nvapi-" + "cF3ozXxPLIGCErCfIQsSaOUJESa1tAS"


def _fmt(msg: str) -> str:
    record = logging.LogRecord("t", logging.ERROR, __file__, 1, msg, None, None)
    return JsonFormatter().format(record)


def test_output_is_valid_json() -> None:
    parsed = json.loads(_fmt("обычное сообщение"))
    assert parsed["msg"] == "обычное сообщение"
    assert parsed["level"] == "ERROR"


def test_redacts_telegram_token_inside_url() -> None:
    """Основной путь утечки: httpx кладёт полный URL в текст исключения.

    Токен идёт как `/bot<token>`, поэтому границы слова перед ним нет.
    """
    out = _fmt(f"сбой запроса к https://api.telegram.org/bot{TOKEN}/getMe")
    assert TOKEN not in out
    assert "REDACTED" in out


def test_redacts_bare_telegram_token() -> None:
    out = _fmt(f"token={TOKEN}")
    assert TOKEN not in out


def test_keeps_ordinary_numbers() -> None:
    """Обычные числа и время не должны вычищаться как секреты."""
    out = _fmt("обработано 1234567890 записей за 12:30")
    assert "1234567890" in out
    assert "REDACTED" not in out


def test_redacts_nvidia_key() -> None:
    out = _fmt(f"Authorization: Bearer {NVKEY}")
    assert NVKEY not in out
    assert "REDACTED" in out


def test_timestamp_is_timezone_aware() -> None:
    """Наивных дат в проекте нет — включая метки логов."""
    parsed = json.loads(_fmt("x"))
    assert parsed["ts"].endswith("+00:00")
