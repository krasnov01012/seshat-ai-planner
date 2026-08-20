"""Конфигурация приложения.

Все секреты — `SecretStr`, чтобы они не попадали в логи, репрезентации объектов
и трейсбеки. Значение достаётся явным `.get_secret_value()` в точке использования.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    telegram_bot_token: SecretStr
    telegram_owner_id: int

    # --- NVIDIA NIM ---
    # Обоснование выбора моделей и порядка запасных — docs/AI_MODELS.md
    nvidia_api_key: SecretStr = SecretStr("")
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model_primary: str = "nvidia/nemotron-3-super-120b-a12b"
    nvidia_model_fallback: str = "nvidia/nvidia-nemotron-nano-9b-v2"
    nvidia_model_fallback_2: str = "z-ai/glm-5.2"
    nvidia_timeout_s: float = Field(default=25.0, gt=0)
    #: Общий wall-clock budget включает очередь, retry, backoff и fallback.
    nvidia_total_timeout_s: float = Field(default=75.0, gt=0)
    nvidia_max_retries: int = Field(default=3, ge=0)
    # Замеры показали 429 уже при 8 параллельных запросах и 12/12 успеха
    # последовательно. Поднимать значение без нового прогона stress-теста нельзя.
    nvidia_max_concurrency: int = Field(default=1, ge=1)

    # --- HTTP API ---
    #: Внутри контейнера слушаем все интерфейсы; docker-compose.yml публикует
    #: порт хоста только на loopback.
    api_host: str = "0.0.0.0"
    api_port: int = 8082
    #: Bearer-токен. Пустой означает, что защищённые ручки отдают 503,
    #: а не открытый доступ без проверки.
    api_token: SecretStr = SecretStr("")

    # --- Готовность к работе за обратным прокси ---
    # Внешняя публикация остаётся настройкой окружения, а не изменением кода.
    #
    #: Внешний адрес, например https://planner.example.com. Нужен для
    #: абсолютных ссылок и для будущего перехода Telegram на webhook.
    public_base_url: str = ""
    #: Доверять заголовкам X-Forwarded-* можно только когда перед сервисом
    #: действительно стоит прокси, иначе клиент подделает свой IP и протокол.
    trust_proxy_headers: bool = False
    #: Кому доверяем X-Forwarded-*. Значение по умолчанию годится только
    #: в паре с trust_proxy_headers=false.
    forwarded_allow_ips: str = "127.0.0.1"
    #: Разрешённые Origin для будущего веб-клиента, через запятую.
    #: Пусто = CORS выключен.
    #:
    #: Тип именно `str`, а не `list[str]`: для списочных полей
    #: pydantic-settings пытается разобрать значение переменной окружения как
    #: JSON ещё до валидаторов, и пустая строка роняет запуск целиком.
    #: Разбираем сами в `cors_origin_list`.
    cors_origins: str = ""

    # --- База данных ---
    database_url: str

    # --- Планировщик ---
    tick_interval_s: int = Field(default=30, ge=1)
    materialize_horizon_days: int = Field(default=14, ge=1)
    late_delivery_threshold_min: int = Field(default=30, ge=0)
    default_event_reminders_min: tuple[int, ...] = (15,)
    default_task_reminders_min: tuple[int, ...] = (120,)
    default_task_morning_local: dt.time = dt.time(8, 0)
    default_routine_reminders_min: tuple[int, ...] = ()
    delivery_batch_size: int = Field(default=20, ge=1, le=100)
    delivery_retry_base_s: int = Field(default=30, ge=1)
    delivery_retry_max_s: int = Field(default=900, ge=1)
    delivery_max_attempts: int = Field(default=5, ge=1)
    important_repeat_interval_min: int = Field(default=15, ge=1)
    important_repeat_max: int = Field(default=3, ge=0, le=10)
    active_context_ttl_min: int = Field(default=180, ge=1)
    scheduler_shutdown_timeout_s: int = Field(default=20, ge=1)

    # --- Умолчания пользователя ---
    default_tz: str = "Europe/Moscow"

    # --- Прочее ---
    log_level: str = "INFO"
    env: str = "dev"

    @field_validator("default_tz")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"неизвестная таймзона IANA: {v!r}") from exc
        return v

    @field_validator("nvidia_base_url")
    @classmethod
    def _nim_requires_https(cls, v: str) -> str:
        parsed = urlsplit(v)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("nvidia_base_url должен быть абсолютным HTTPS URL")
        return v.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url должен использовать драйвер postgresql+asyncpg")
        return v

    @field_validator(
        "default_event_reminders_min",
        "default_task_reminders_min",
        "default_routine_reminders_min",
    )
    @classmethod
    def _positive_default_reminders(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value <= 0 for value in values):
            raise ValueError("default reminder offsets must be positive")
        return tuple(sorted(set(values), reverse=True))

    @field_validator("default_task_morning_local")
    @classmethod
    def _naive_task_morning(cls, value: dt.time) -> dt.time:
        if value.tzinfo is not None:
            raise ValueError("default task morning must be local wall time without offset")
        return value

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
