"""Проверки схемы на настоящей PostgreSQL.

Главная из них — `test_duplicate_notification_is_rejected`. Требование «после
перезапуска VPS не потерять и не продублировать напоминания» держится ровно
на одном ограничении уникальности; если оно перестанет работать, проект
тихо начнёт слать по два уведомления.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from seshat.db.enums import (
    EntryKind,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
)
from seshat.db.models import Entry, Notification, Occurrence, User, UserSettings

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)

UTC = dt.UTC


async def _user(session) -> User:
    user = User(telegram_id=123456789)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id))
    await session.flush()
    return user


async def _occurrence(session, user: User, *, at: dt.datetime) -> Occurrence:
    entry = Entry(
        user_id=user.id,
        kind=EntryKind.EVENT,
        title="Собеседование с А2",
        start_at_utc=at,
        tz="Europe/Moscow",
        local_time=dt.time(15, 0),
    )
    session.add(entry)
    await session.flush()
    occ = Occurrence(entry_id=entry.id, user_id=user.id, planned_at_utc=at)
    session.add(occ)
    await session.flush()
    return occ


async def test_duplicate_notification_is_rejected(session) -> None:
    """Повторный прогон планировщика получает конфликт, а не второе уведомление."""
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)

    session.add(
        Notification(
            occurrence_id=occ.id, user_id=user.id, fire_at_utc=at, kind=NotificationKind.MAIN
        )
    )
    await session.flush()

    session.add(
        Notification(
            occurrence_id=occ.id, user_id=user.id, fire_at_utc=at, kind=NotificationKind.MAIN
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_different_kinds_at_same_moment_are_allowed(session) -> None:
    """Основное и предварительное могут совпасть по времени — это не дубль."""
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)

    for kind in (NotificationKind.MAIN, NotificationKind.PRE, NotificationKind.REPEAT):
        session.add(Notification(occurrence_id=occ.id, user_id=user.id, fire_at_utc=at, kind=kind))
    await session.flush()

    count = len((await session.execute(select(Notification))).scalars().all())
    assert count == 3


async def test_materializer_cannot_duplicate_occurrence(session) -> None:
    """Повторный прогон материализатора не создаёт второй экземпляр на тот же момент."""
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)

    session.add(Occurrence(entry_id=occ.entry_id, user_id=user.id, planned_at_utc=at))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_timestamps_come_back_timezone_aware(session) -> None:
    """Наивных дат быть не должно даже после обхода через БД."""
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)
    await session.commit()

    session.expunge_all()
    loaded = (await session.execute(select(Occurrence).where(Occurrence.id == occ.id))).scalar_one()
    assert loaded.planned_at_utc.tzinfo is not None
    assert loaded.planned_at_utc == at


async def test_routine_requires_rrule(session) -> None:
    """Рутина без правила повторения — не рутина."""
    user = await _user(session)
    session.add(
        Entry(
            user_id=user.id,
            kind=EntryKind.ROUTINE,
            title="Принять добавки",
            tz="Europe/Moscow",
            local_time=dt.time(8, 0),
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_insane_duration_is_rejected(session) -> None:
    user = await _user(session)
    session.add(
        Entry(
            user_id=user.id,
            kind=EntryKind.TASK,
            title="Бесконечная задача",
            tz="Europe/Moscow",
            duration_min=5000,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_local_time_survives_for_recalculation(session) -> None:
    """После переезда рутину пересчитывают из local_time, а не из UTC-момента.

    Проверяем, что «каждый день в 8:00» хранит именно 08:00, независимо
    от того, в какой момент UTC это попало.
    """
    user = await _user(session)
    entry = Entry(
        user_id=user.id,
        kind=EntryKind.ROUTINE,
        title="Принять добавки",
        rrule="FREQ=DAILY",
        tz="Europe/Moscow",
        local_time=dt.time(8, 0),
    )
    session.add(entry)
    await session.commit()

    session.expunge_all()
    loaded = (await session.execute(select(Entry).where(Entry.id == entry.id))).scalar_one()
    assert loaded.local_time == dt.time(8, 0)
    assert loaded.tz == "Europe/Moscow"


async def test_defaults_follow_documented_decisions(session) -> None:
    """Тихие часы 23:00–08:00 и обязательное подтверждение — из docs/DECISIONS.md."""
    user = await _user(session)
    await session.commit()

    session.expunge_all()
    settings = (
        await session.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    ).scalar_one()
    assert settings.quiet_from == dt.time(23, 0)
    assert settings.quiet_to == dt.time(8, 0)
    assert settings.confirm_before_save is True
    assert settings.digest_enabled is False
    assert settings.tz == "Europe/Moscow"


async def test_soft_delete_keeps_row(session) -> None:
    """Удаление мягкое: строка остаётся, чтобы правку можно было откатить."""
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)

    entry = (await session.execute(select(Entry).where(Entry.id == occ.entry_id))).scalar_one()
    entry.deleted_at = dt.datetime.now(UTC)
    await session.commit()

    still_there = (await session.execute(select(Entry).where(Entry.id == entry.id))).scalar_one()
    assert still_there.deleted_at is not None
    assert still_there.title == "Собеседование с А2"


async def test_occurrence_status_transitions_are_stored(session) -> None:
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)

    occ.status = OccurrenceStatus.MOVED
    occ.moved_count += 1
    await session.commit()

    session.expunge_all()
    loaded = (await session.execute(select(Occurrence).where(Occurrence.id == occ.id))).scalar_one()
    assert loaded.status is OccurrenceStatus.MOVED
    assert loaded.moved_count == 1


async def test_notification_defaults_to_pending_and_loud(session) -> None:
    user = await _user(session)
    at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    occ = await _occurrence(session, user, at=at)

    n = Notification(
        occurrence_id=occ.id, user_id=user.id, fire_at_utc=at, kind=NotificationKind.MAIN
    )
    session.add(n)
    await session.commit()

    session.expunge_all()
    loaded = (
        await session.execute(select(Notification).where(Notification.id == n.id))
    ).scalar_one()
    assert loaded.status is NotificationStatus.PENDING
    assert loaded.silent is False
