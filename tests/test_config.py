"""Тесты конфигурации: валидация и нераскрытие секретов."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from seshat.config import Settings

BASE = {
    "telegram_bot_token": "123456789:" + "AAtest-token-value-for-tests-only-xxxx",
    "telegram_owner_id": 42,
    "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
}


def make(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


def test_defaults_match_documented_choices() -> None:
    """Умолчания должны совпадать с docs/AI_MODELS.md и docs/DECISIONS.md."""
    s = make()
    assert s.nvidia_model_primary == "nvidia/nemotron-3-super-120b-a12b"
    assert s.nvidia_model_fallback == "nvidia/nvidia-nemotron-nano-9b-v2"
    assert s.default_tz == "Europe/Moscow"
    assert s.tick_interval_s == 30
    assert s.materialize_horizon_days == 14
    assert s.late_delivery_threshold_min == 30
    assert s.default_event_reminders_min == (15,)
    assert s.default_task_reminders_min == (120,)
    assert s.default_task_morning_local.isoformat(timespec="minutes") == "08:00"
    assert s.default_routine_reminders_min == ()
    assert s.important_repeat_interval_min == 15
    assert s.important_repeat_max == 3
    assert s.scheduler_shutdown_timeout_s == 20


def test_concurrency_default_is_one() -> None:
    """Стресс-тест: 12/12 последовательно, 10/24 при восьми параллельных.

    Значение по умолчанию поднимать нельзя без нового прогона
    tools/stress_nvidia_limits.py — см. docs/AI_MODELS.md.
    """
    assert make().nvidia_max_concurrency == 1


def test_nvidia_endpoint_must_use_https() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        make(nvidia_base_url="http://nim.example/v1")


def test_nvidia_limits_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make(nvidia_timeout_s=0)
    with pytest.raises(ValidationError):
        make(nvidia_total_timeout_s=0)
    with pytest.raises(ValidationError):
        make(nvidia_max_concurrency=0)


def test_empty_cors_origins_does_not_break_startup() -> None:
    """`CORS_ORIGINS=` в файле окружения — обычный случай, а не ошибка.

    Регрессия: поле было объявлено как `list[str]`, и pydantic-settings пытался
    разобрать пустую строку как JSON ещё до валидаторов. Локально не
    воспроизводилось, потому что переменной не было в старом `.env`;
    на сервере при первом же деплое упали оба контейнера.
    """
    assert make(cors_origins="").cors_origin_list == []


def test_cors_origins_parsed_from_comma_separated() -> None:
    parsed = make(cors_origins="https://a.example, https://b.example").cors_origin_list
    assert parsed == ["https://a.example", "https://b.example"]


def test_proxy_headers_are_not_trusted_by_default() -> None:
    """Без прокси перед сервисом доверие к X-Forwarded-* позволяет подделать IP."""
    assert make().trust_proxy_headers is False


def test_rejects_unknown_timezone() -> None:
    with pytest.raises(ValidationError, match="неизвестная таймзона"):
        make(default_tz="Europe/Atlantis")


def test_accepts_relocation_timezone() -> None:
    """Переезд запланирован — целевые таймзоны обязаны проходить валидацию."""
    assert make(default_tz="Europe/Amsterdam").default_tz == "Europe/Amsterdam"


def test_rejects_sync_database_driver() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        make(database_url="postgresql://u:p@localhost:5432/db")


def test_secrets_are_not_exposed_in_repr() -> None:
    """Токен не должен появляться в repr, str и логах."""
    s = make(nvidia_api_key="nvapi-" + "secret-value-for-tests")
    dumped = repr(s) + str(s) + str(s.model_dump())
    assert "AAtest-token-value" not in dumped
    assert "nvapi-secret-value" not in dumped
    assert s.telegram_bot_token.get_secret_value().startswith("123456789:")
