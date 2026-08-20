"""Occurrence materialization and durable scheduler tests on PostgreSQL 16."""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest
import time_machine
from sqlalchemy import select

from seshat.db.enums import EntryKind, NotificationKind, NotificationStatus, OccurrenceStatus
from seshat.db.models import ActiveContext, Entry, Notification, Occurrence, User, UserSettings
from seshat.domain.scheduling import (
    ReminderDefaults,
    materialize_occurrences,
    resolve_wall_time,
    schedule_notifications,
    skip_occurrence,
)

UTC = dt.UTC
requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL scheduling tests",
)


def test_resolve_wall_time_has_explicit_dst_policy() -> None:
    spring = resolve_wall_time(dt.datetime(2026, 3, 29, 2, 30), "Europe/Amsterdam")
    assert spring.astimezone(ZoneInfo("Europe/Amsterdam")) == dt.datetime(
        2026, 3, 29, 3, 30, tzinfo=ZoneInfo("Europe/Amsterdam")
    )

    autumn = resolve_wall_time(dt.datetime(2026, 10, 25, 2, 30), "Europe/Amsterdam")
    local_autumn = autumn.astimezone(ZoneInfo("Europe/Amsterdam"))
    assert local_autumn.hour == 2
    assert local_autumn.minute == 30
    assert local_autumn.fold == 0
    assert autumn == dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_resolve_wall_time_rejects_aware_input() -> None:
    with pytest.raises(ValueError, match="naive"):
        resolve_wall_time(dt.datetime(2026, 3, 29, 2, 30, tzinfo=UTC), "Europe/Amsterdam")


async def _user(session, telegram_id: int = 123456789) -> User:
    user = User(telegram_id=telegram_id)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, tz="Europe/Moscow"))
    await session.flush()
    return user


@requires_postgres
@pytest.mark.asyncio
async def test_materializer_is_idempotent_and_handles_spring_gap(session) -> None:
    user = await _user(session)
    routine = Entry(
        user_id=user.id,
        kind=EntryKind.ROUTINE,
        title="Night routine",
        start_at_utc=dt.datetime(2026, 3, 27, 1, 30, tzinfo=UTC),
        rrule="FREQ=DAILY",
        tz="Europe/Amsterdam",
        local_time=dt.time(2, 30),
    )
    session.add(routine)
    await session.flush()

    with time_machine.travel("2026-03-27 00:00:00+00:00", tick=False):
        first = await materialize_occurrences(
            session,
            now_utc=dt.datetime.now(UTC),
            horizon_days=4,
            lookback_minutes=0,
        )
        second = await materialize_occurrences(
            session,
            now_utc=dt.datetime.now(UTC),
            horizon_days=4,
            lookback_minutes=0,
        )

    assert len(first.created_occurrence_ids) == 4
    assert second.created_occurrence_ids == ()
    assert second.existing_count == 4
    occurrences = (
        (
            await session.execute(
                select(Occurrence)
                .where(Occurrence.entry_id == routine.id)
                .order_by(Occurrence.planned_at_utc)
            )
        )
        .scalars()
        .all()
    )
    dst_day = next(
        item
        for item in occurrences
        if item.planned_at_utc.astimezone(ZoneInfo("Europe/Amsterdam")).date()
        == dt.date(2026, 3, 29)
    )
    assert dst_day.planned_at_utc.astimezone(ZoneInfo("Europe/Amsterdam")).time().replace(
        tzinfo=None
    ) == dt.time(3, 30)


@requires_postgres
@pytest.mark.asyncio
async def test_scheduler_applies_defaults_and_is_idempotent(session) -> None:
    user = await _user(session)
    planned = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    entries = [
        Entry(
            user_id=user.id,
            kind=EntryKind.EVENT,
            title="Event",
            start_at_utc=planned,
            tz="Europe/Moscow",
            local_time=dt.time(15, 0),
        ),
        Entry(
            user_id=user.id,
            kind=EntryKind.TASK,
            title="Task",
            due_at_utc=planned,
            tz="Europe/Moscow",
            local_time=dt.time(15, 0),
        ),
        Entry(
            user_id=user.id,
            kind=EntryKind.ROUTINE,
            title="Routine",
            start_at_utc=planned,
            rrule="FREQ=DAILY",
            tz="Europe/Moscow",
            local_time=dt.time(15, 0),
        ),
    ]
    session.add_all(entries)
    await session.flush()
    occurrences = [
        Occurrence(entry_id=entry.id, user_id=user.id, planned_at_utc=planned) for entry in entries
    ]
    session.add_all(occurrences)
    await session.flush()

    defaults = ReminderDefaults()
    with time_machine.travel("2026-08-05 04:00:00+00:00", tick=False):
        first = await schedule_notifications(
            session,
            now_utc=dt.datetime.now(UTC),
            defaults=defaults,
            occurrence_ids=[item.id for item in occurrences],
        )
        second = await schedule_notifications(
            session,
            now_utc=dt.datetime.now(UTC),
            defaults=defaults,
            occurrence_ids=[item.id for item in occurrences],
        )

    assert len(first.created_notification_ids) == 6
    assert second.created_notification_ids == ()
    assert second.existing_count == 6
    rows = (
        (
            await session.execute(
                select(Notification).order_by(Notification.occurrence_id, Notification.fire_at_utc)
            )
        )
        .scalars()
        .all()
    )
    by_occurrence = {
        occurrence.id: [row for row in rows if row.occurrence_id == occurrence.id]
        for occurrence in occurrences
    }
    assert {(row.kind, row.fire_at_utc) for row in by_occurrence[occurrences[0].id]} == {
        (NotificationKind.PRE, dt.datetime(2026, 8, 5, 11, 45, tzinfo=UTC)),
        (NotificationKind.MAIN, planned),
    }
    assert {(row.kind, row.fire_at_utc) for row in by_occurrence[occurrences[1].id]} == {
        (NotificationKind.PRE, dt.datetime(2026, 8, 5, 5, 0, tzinfo=UTC)),
        (NotificationKind.PRE, dt.datetime(2026, 8, 5, 10, 0, tzinfo=UTC)),
        (NotificationKind.MAIN, planned),
    }
    assert [(row.kind, row.fire_at_utc) for row in by_occurrence[occurrences[2].id]] == [
        (NotificationKind.MAIN, planned)
    ]
    assert all(row.silent is False for row in rows)


@requires_postgres
@pytest.mark.asyncio
async def test_explicit_zero_means_main_only_and_cancelled_can_be_reactivated(session) -> None:
    user = await _user(session)
    planned = dt.datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    entry = Entry(
        user_id=user.id,
        kind=EntryKind.EVENT,
        title="Main only",
        start_at_utc=planned,
        tz="Europe/Moscow",
        local_time=dt.time(15, 0),
        reminders_min_before=[0],
    )
    session.add(entry)
    await session.flush()
    occurrence = Occurrence(entry_id=entry.id, user_id=user.id, planned_at_utc=planned)
    session.add(occurrence)
    await session.flush()

    first = await schedule_notifications(
        session,
        now_utc=dt.datetime(2026, 8, 5, 9, 0, tzinfo=UTC),
        defaults=ReminderDefaults(),
        occurrence_ids=[occurrence.id],
    )
    assert len(first.created_notification_ids) == 1
    notification = await session.get(Notification, first.created_notification_ids[0])
    assert notification is not None
    notification.status = NotificationStatus.CANCELLED
    await session.flush()

    replay = await schedule_notifications(
        session,
        now_utc=dt.datetime(2026, 8, 5, 9, 1, tzinfo=UTC),
        defaults=ReminderDefaults(),
        occurrence_ids=[occurrence.id],
    )
    assert replay.reactivated_notification_ids == (notification.id,)
    assert notification.status is NotificationStatus.PENDING


@requires_postgres
@pytest.mark.asyncio
async def test_skip_one_routine_occurrence_survives_reconciliation(session) -> None:
    user = await _user(session)
    routine = Entry(
        user_id=user.id,
        kind=EntryKind.ROUTINE,
        title="Exercise",
        start_at_utc=dt.datetime(2026, 8, 5, 5, 0, tzinfo=UTC),
        rrule="FREQ=DAILY",
        tz="Europe/Moscow",
        local_time=dt.time(8, 0),
    )
    session.add(routine)
    await session.flush()
    now = dt.datetime(2026, 8, 5, 0, 0, tzinfo=UTC)
    materialized = await materialize_occurrences(
        session, now_utc=now, horizon_days=3, lookback_minutes=0
    )
    await schedule_notifications(
        session,
        now_utc=now,
        defaults=ReminderDefaults(),
        occurrence_ids=materialized.created_occurrence_ids,
    )
    occurrences = (
        (
            await session.execute(
                select(Occurrence)
                .where(Occurrence.entry_id == routine.id)
                .order_by(Occurrence.planned_at_utc)
            )
        )
        .scalars()
        .all()
    )
    target = occurrences[1]
    target_notification = await session.scalar(
        select(Notification).where(Notification.occurrence_id == target.id)
    )
    assert target_notification is not None
    session.add(
        ActiveContext(
            user_id=user.id,
            occurrence_id=target.id,
            notification_id=target_notification.id,
            expires_at_utc=now + dt.timedelta(hours=3),
        )
    )
    await session.flush()

    changed = await skip_occurrence(
        session,
        user_id=user.id,
        occurrence_id=target.id,
        now_utc=now,
    )
    replay = await skip_occurrence(
        session,
        user_id=user.id,
        occurrence_id=target.id,
        now_utc=now,
    )
    await materialize_occurrences(session, now_utc=now, horizon_days=3, lookback_minutes=0)

    assert changed.changed is True
    assert replay.changed is False
    await session.refresh(target)
    assert target.status is OccurrenceStatus.SKIPPED
    statuses = (
        (
            await session.execute(
                select(Notification.status).where(Notification.occurrence_id == target.id)
            )
        )
        .scalars()
        .all()
    )
    assert statuses == [NotificationStatus.CANCELLED]
    assert await session.get(ActiveContext, user.id) is None
    all_occurrences = (
        (
            await session.execute(
                select(Occurrence)
                .where(Occurrence.entry_id == routine.id)
                .order_by(Occurrence.planned_at_utc)
            )
        )
        .scalars()
        .all()
    )
    assert len(all_occurrences) == len(occurrences)
    assert [item.status for item in all_occurrences].count(OccurrenceStatus.SKIPPED) == 1


@requires_postgres
@pytest.mark.asyncio
async def test_naive_scheduler_clock_is_rejected(session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        await materialize_occurrences(
            session,
            now_utc=dt.datetime(2026, 8, 5, 12, 0),
            horizon_days=14,
        )
