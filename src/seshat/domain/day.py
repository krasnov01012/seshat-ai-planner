"""Read model текущего дня пользователя."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.enums import EntryKind, EntryStatus, NotificationStatus, OccurrenceStatus
from seshat.db.models import Entry, Notification, Occurrence, UserSettings
from seshat.domain import DomainError
from seshat.domain.delivery import completed_quiet_window


@dataclass(frozen=True, slots=True)
class MyDayItem:
    occurrence_id: int
    entry_id: int
    kind: EntryKind
    title: str
    planned_at_utc: dt.datetime
    planned_at_local: dt.datetime
    status: OccurrenceStatus


@dataclass(frozen=True, slots=True)
class MyDayNightItem:
    notification_id: int
    occurrence_id: int
    title: str
    sent_at_utc: dt.datetime
    sent_at_local: dt.datetime


@dataclass(frozen=True, slots=True)
class MyDay:
    local_date: dt.date
    tz: str
    missed: tuple[MyDayItem, ...]
    items: tuple[MyDayItem, ...]
    night: tuple[MyDayNightItem, ...] = ()


async def get_my_day(
    session: AsyncSession,
    user_id: int,
    *,
    now_utc: dt.datetime,
) -> MyDay:
    """Возвращает текущий локальный день; пропущенное вынесено наверх."""
    _require_aware(now_utc)
    settings = await session.get(UserSettings, user_id)
    if settings is None:
        raise DomainError("user settings not found")

    zone = ZoneInfo(settings.tz)
    local_date = now_utc.astimezone(zone).date()
    start_local = dt.datetime.combine(local_date, dt.time.min, tzinfo=zone)
    end_local = dt.datetime.combine(local_date + dt.timedelta(days=1), dt.time.min, tzinfo=zone)
    start_utc = start_local.astimezone(dt.UTC)
    end_utc = end_local.astimezone(dt.UTC)
    night_start_utc, night_end_utc = completed_quiet_window(
        local_date,
        tz=settings.tz,
        quiet_from=settings.quiet_from,
        quiet_to=settings.quiet_to,
    )

    rows = (
        await session.execute(
            select(Occurrence, Entry)
            .join(Entry, Entry.id == Occurrence.entry_id)
            .where(
                Occurrence.user_id == user_id,
                Occurrence.planned_at_utc >= start_utc,
                Occurrence.planned_at_utc < end_utc,
                Occurrence.status != OccurrenceStatus.SKIPPED,
                Occurrence.status != OccurrenceStatus.MOVED,
                Entry.status == EntryStatus.ACTIVE,
                Entry.deleted_at.is_(None),
            )
            .order_by(Occurrence.planned_at_utc, Occurrence.id)
        )
    ).all()
    converted = tuple(
        MyDayItem(
            occurrence_id=occurrence.id,
            entry_id=entry.id,
            kind=entry.kind,
            title=entry.title,
            planned_at_utc=occurrence.planned_at_utc,
            planned_at_local=occurrence.planned_at_utc.astimezone(zone),
            status=occurrence.status,
        )
        for occurrence, entry in rows
    )
    night_rows = (
        await session.execute(
            select(Notification.id, Occurrence.id, Entry.title, Notification.sent_at_utc)
            .join(Occurrence, Occurrence.id == Notification.occurrence_id)
            .join(Entry, Entry.id == Occurrence.entry_id)
            .where(
                Notification.user_id == user_id,
                Notification.status == NotificationStatus.SENT,
                Notification.silent.is_(True),
                Notification.sent_at_utc >= night_start_utc,
                Notification.sent_at_utc < night_end_utc,
            )
            .order_by(Notification.sent_at_utc, Notification.id)
        )
    ).all()
    return MyDay(
        local_date=local_date,
        tz=settings.tz,
        missed=tuple(item for item in converted if item.status is OccurrenceStatus.MISSED),
        items=tuple(item for item in converted if item.status is not OccurrenceStatus.MISSED),
        night=tuple(
            MyDayNightItem(
                notification_id=notification_id,
                occurrence_id=occurrence_id,
                title=title,
                sent_at_utc=sent_at_utc,
                sent_at_local=sent_at_utc.astimezone(zone),
            )
            for notification_id, occurrence_id, title, sent_at_utc in night_rows
        ),
    )


def _require_aware(value: dt.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


__all__ = ["MyDay", "MyDayItem", "MyDayNightItem", "get_my_day"]
