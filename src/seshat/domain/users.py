"""Сервис пользователей и настроек.

Слой домена не знает ни про HTTP, ни про Telegram. Это единственное место,
где живёт бизнес-логика: и API, и бот — тонкие адаптеры поверх него.
Правило простое — если логика понадобилась в двух местах, она принадлежит сюда.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.models import User, UserSettings


class DomainError(Exception):
    """Нарушение правила предметной области. API отдаёт как 400."""


class UnknownTimezoneError(DomainError):
    pass


def validate_tz(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UnknownTimezoneError(f"неизвестная таймзона IANA: {name!r}") from exc
    return name


async def get_or_create_user(
    session: AsyncSession, telegram_id: int, *, default_tz: str = "Europe/Moscow"
) -> User:
    """Идемпотентно: повторный вызов возвращает того же пользователя."""
    existing = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    user = User(telegram_id=telegram_id)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, tz=validate_tz(default_tz)))
    await session.flush()
    return user


async def find_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    """Чистое чтение для preview: в отличие от get_or_create ничего не пишет."""
    return (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()


async def get_settings(session: AsyncSession, user_id: int) -> UserSettings:
    return (
        await session.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one()


async def update_quiet_hours(
    session: AsyncSession, user_id: int, quiet_from: dt.time, quiet_to: dt.time
) -> UserSettings:
    """Тихие часы.

    Совпадение границ запрещено: это означало бы либо круглосуточную тишину,
    либо её отсутствие — и то и другое лучше выражать явно, а не вырожденным
    интервалом. Интервал через полночь (23:00–08:00) — норма, а не ошибка.
    """
    if quiet_from == quiet_to:
        raise DomainError("начало и конец тихих часов не могут совпадать")

    settings = await get_settings(session, user_id)
    settings.quiet_from = quiet_from
    settings.quiet_to = quiet_to
    await session.flush()
    return settings


def is_quiet(moment_local: dt.time, quiet_from: dt.time, quiet_to: dt.time) -> bool:
    """Попадает ли местное время в тихие часы.

    Интервал почти всегда проходит через полночь, поэтому наивное сравнение
    `quiet_from <= t < quiet_to` здесь неверно и даёт ложь для всего диапазона.
    """
    if quiet_from < quiet_to:
        return quiet_from <= moment_local < quiet_to
    return moment_local >= quiet_from or moment_local < quiet_to
