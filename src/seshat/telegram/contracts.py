"""Узкая граница зависимостей между aiogram и доменным слоем."""

from __future__ import annotations

import datetime as dt
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ManualEntryInput:
    """JSON-совместимые поля, собранные ручной Telegram-формой."""

    kind: str
    title: str
    local_date: dt.date
    local_time: dt.time
    duration_min: int | None = None
    recurrence: JsonObject | None = None
    reminders_min_before: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Ready:
    """Проверенная карточка, которую можно показать для подтверждения."""

    normalized: JsonObject


@dataclass(frozen=True, slots=True)
class Clarification:
    prompt: str = "Уточни план: напиши точные дату и время."


@dataclass(frozen=True, slots=True)
class ManualFallback:
    prompt: str = "Не смог разобрать. Заполним по шагам?"


type PreparationResult = Ready | Clarification | ManualFallback


class PrepareText(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        user_id: int,
        text: str,
        *,
        tz: str,
        now_utc: dt.datetime,
    ) -> PreparationResult: ...


type PrepareManual = Callable[
    [ManualEntryInput, str, dt.datetime],
    Ready | Mapping[str, Any] | Any | Awaitable[Ready | Mapping[str, Any] | Any],
]
type CreateEntry = Callable[[AsyncSession, int, Mapping[str, Any], str], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class TelegramDependencies:
    session_factory: async_sessionmaker[AsyncSession]
    prepare_text: PrepareText
    prepare_manual: PrepareManual
    create_entry: CreateEntry
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)
    default_tz: str = "Europe/Moscow"


async def resolve(value: Any) -> Any:
    """Ожидает внедрённую async-функцию, но принимает и чистый domain helper."""
    if inspect.isawaitable(value):
        return await value
    return value


def as_json_object(value: Any) -> JsonObject:
    """Сериализует доменный Pydantic-объект перед записью в FSM."""
    if isinstance(value, Ready):
        return dict(value.normalized)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("prepared entry must be a Pydantic model or mapping")
