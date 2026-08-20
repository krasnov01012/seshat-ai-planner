"""Атомарный workflow: подтверждение записи сразу получает расписание."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.enums import OccurrenceStatus
from seshat.db.models import Occurrence, UserSettings
from seshat.domain.entries import CreateEntryResult, EntryDraft, create_entry
from seshat.domain.scheduling import (
    ReminderDefaults,
    materialize_occurrences,
    schedule_notifications,
)
from seshat.domain.users import DomainError


async def create_planned_entry(
    session: AsyncSession,
    user_id: int,
    draft: EntryDraft,
    *,
    confirmation_id: uuid.UUID,
    now_utc: dt.datetime,
    horizon_days: int,
    late_lookback_minutes: int,
    defaults: ReminderDefaults,
) -> CreateEntryResult:
    """Создаёт запись и её ближайшие уведомления в одной транзакции.

    Немедленная материализация нужна не только для скорости: иначе запись,
    созданная сразу после часового reconciliation и назначенная в пределах
    часа, могла бы попасть за пределы late-window следующего запуска.
    """
    settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id).with_for_update()
    )
    if settings is None:
        raise DomainError("настройки пользователя не найдены")
    if draft.tz != settings.tz:
        raise DomainError("карточка устарела: часовой пояс уже изменился")

    result = await create_entry(
        session,
        user_id,
        draft,
        confirmation_id=confirmation_id,
    )
    await materialize_occurrences(
        session,
        now_utc=now_utc,
        horizon_days=horizon_days,
        lookback_minutes=late_lookback_minutes,
        user_id=user_id,
        entry_id=result.entry.id,
    )
    occurrence_ids = tuple(
        (
            await session.execute(
                select(Occurrence.id).where(
                    Occurrence.entry_id == result.entry.id,
                    Occurrence.status == OccurrenceStatus.PENDING,
                )
            )
        )
        .scalars()
        .all()
    )
    await schedule_notifications(
        session,
        now_utc=now_utc,
        defaults=defaults,
        occurrence_ids=occurrence_ids,
    )
    return result


__all__ = ["create_planned_entry"]
