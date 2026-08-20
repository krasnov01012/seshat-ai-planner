"""Доменный контракт подтверждённой смены таймзоны."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import time_machine
from sqlalchemy import func, select

from seshat.db.enums import (
    EntryKind,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
)
from seshat.db.models import AuditLog, Entry, Notification, Occurrence, TzChange
from seshat.domain.timezones import (
    TimezoneConflictError,
    TimezoneReviewDecision,
    confirm_timezone_change,
    list_timezone_reviews,
    preview_timezone_change,
    review_timezone_entry,
)
from seshat.domain.users import get_or_create_user

NOW = dt.datetime(2026, 8, 3, 9, 0, tzinfo=dt.UTC)


async def _user(session):
    return await get_or_create_user(session, 123456789, default_tz="Europe/Moscow")


async def _entry(
    session,
    user_id: int,
    *,
    kind: EntryKind,
    title: str,
    moment: dt.datetime,
) -> Entry:
    entry = Entry(
        user_id=user_id,
        kind=kind,
        title=title,
        start_at_utc=moment if kind is not EntryKind.TASK else None,
        due_at_utc=moment if kind is EntryKind.TASK else None,
        rrule="FREQ=DAILY" if kind is EntryKind.ROUTINE else None,
        tz="Europe/Moscow",
        local_time=dt.time(8, 0) if kind is EntryKind.ROUTINE else dt.time(15, 0),
        reminders_min_before=[15],
    )
    session.add(entry)
    await session.flush()
    return entry


async def _occurrence(session, entry: Entry) -> Occurrence:
    moment = entry.start_at_utc or entry.due_at_utc
    assert moment is not None
    occurrence = Occurrence(
        entry_id=entry.id,
        user_id=entry.user_id,
        planned_at_utc=moment,
        status=OccurrenceStatus.PENDING,
    )
    session.add(occurrence)
    await session.flush()
    return occurrence


async def test_preview_is_read_only(session) -> None:
    user = await _user(session)
    before = await session.scalar(select(func.count()).select_from(TzChange))

    preview = await preview_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        now_utc=NOW,
        confirmation_id=uuid.UUID("e52c47ef-19b5-4b48-8406-15c854a14d1c"),
    )

    assert preview.tz_from == "Europe/Moscow"
    assert preview.tz_to == "Europe/Amsterdam"
    assert preview.confirmation_id == uuid.UUID("e52c47ef-19b5-4b48-8406-15c854a14d1c")
    assert await session.scalar(select(func.count()).select_from(TzChange)) == before


async def test_confirm_is_idempotent_and_stale_preview_conflicts(session) -> None:
    user = await _user(session)
    confirmation = uuid.UUID("282f6baa-813f-4304-bfd5-2521a0a62ac0")

    first = await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=confirmation,
        now_utc=NOW,
    )
    replay = await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=confirmation,
        now_utc=NOW,
    )

    assert first.applied is True
    assert replay.applied is False
    assert replay.change.id == first.change.id
    assert await session.scalar(select(func.count()).select_from(TzChange)) == 1

    with pytest.raises(TimezoneConflictError, match="устарела"):
        await confirm_timezone_change(
            session,
            user.id,
            "Europe/London",
            expected_tz_from="Europe/Moscow",
            confirmation_id=uuid.uuid4(),
            now_utc=NOW,
        )


async def test_routine_rebases_there_and_back_while_event_stays_absolute(session) -> None:
    user = await _user(session)
    routine = await _entry(
        session,
        user.id,
        kind=EntryKind.ROUTINE,
        title="Зарядка",
        moment=dt.datetime(2026, 8, 4, 5, 0, tzinfo=dt.UTC),
    )
    event = await _entry(
        session,
        user.id,
        kind=EntryKind.EVENT,
        title="Собеседование",
        moment=dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC),
    )
    routine_occurrence = await _occurrence(session, routine)
    event_occurrence = await _occurrence(session, event)
    pending = Notification(
        occurrence_id=routine_occurrence.id,
        user_id=user.id,
        fire_at_utc=dt.datetime(2026, 8, 4, 4, 45, tzinfo=dt.UTC),
        kind=NotificationKind.PRE,
        status=NotificationStatus.PENDING,
    )
    sent = Notification(
        occurrence_id=event_occurrence.id,
        user_id=user.id,
        fire_at_utc=dt.datetime(2026, 8, 5, 11, 45, tzinfo=dt.UTC),
        kind=NotificationKind.PRE,
        status=NotificationStatus.SENT,
        sent_at_utc=dt.datetime(2026, 8, 3, 8, 0, tzinfo=dt.UTC),
        telegram_message_id=777,
        silent=True,
    )
    session.add_all([pending, sent])
    await session.flush()
    sent_snapshot = (
        sent.fire_at_utc,
        sent.status,
        sent.sent_at_utc,
        sent.telegram_message_id,
        sent.silent,
    )

    outbound = await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=uuid.uuid4(),
        now_utc=NOW,
    )

    assert routine.start_at_utc == dt.datetime(2026, 8, 4, 6, 0, tzinfo=dt.UTC)
    assert routine_occurrence.planned_at_utc == dt.datetime(2026, 8, 4, 6, 0, tzinfo=dt.UTC)
    assert pending.fire_at_utc == dt.datetime(2026, 8, 4, 5, 45, tzinfo=dt.UTC)
    assert event.start_at_utc == dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    assert (
        sent.fire_at_utc,
        sent.status,
        sent.sent_at_utc,
        sent.telegram_message_id,
        sent.silent,
    ) == sent_snapshot
    assert outbound.review_remaining == 1

    reviewed = await review_timezone_entry(
        session,
        user.id,
        outbound.change.id,
        event.id,
        TimezoneReviewDecision.KEEP_ABSOLUTE,
        now_utc=NOW,
    )
    assert reviewed.review_remaining == 0
    assert event.start_at_utc == dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)

    inbound = await confirm_timezone_change(
        session,
        user.id,
        "Europe/Moscow",
        expected_tz_from="Europe/Amsterdam",
        confirmation_id=uuid.uuid4(),
        now_utc=NOW,
    )
    assert routine.start_at_utc == dt.datetime(2026, 8, 4, 5, 0, tzinfo=dt.UTC)
    assert routine_occurrence.planned_at_utc == dt.datetime(2026, 8, 4, 5, 0, tzinfo=dt.UTC)
    assert event.start_at_utc == dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    inbound_reviews = await list_timezone_reviews(session, user.id, inbound.change.id)
    # После KEEP_ABSOLUTE entry.tz остаётся исторической (Москва), но текущее
    # отображаемое время перед обратным переездом — 14:00 Амстердама.
    assert inbound_reviews[0].keep_local_at_utc == dt.datetime(2026, 8, 5, 11, 0, tzinfo=dt.UTC)
    await review_timezone_entry(
        session,
        user.id,
        inbound.change.id,
        event.id,
        TimezoneReviewDecision.KEEP_LOCAL,
        now_utc=NOW,
    )
    assert event.start_at_utc == dt.datetime(2026, 8, 5, 11, 0, tzinfo=dt.UTC)


async def test_keep_local_moves_event_but_never_mutates_sent_notification(session) -> None:
    user = await _user(session)
    event = await _entry(
        session,
        user.id,
        kind=EntryKind.EVENT,
        title="Интервью на новом месте",
        moment=dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC),
    )
    occurrence = await _occurrence(session, event)
    sent = Notification(
        occurrence_id=occurrence.id,
        user_id=user.id,
        fire_at_utc=dt.datetime(2026, 8, 3, 8, 0, tzinfo=dt.UTC),
        kind=NotificationKind.PRE,
        status=NotificationStatus.SENT,
        sent_at_utc=dt.datetime(2026, 8, 3, 8, 0, tzinfo=dt.UTC),
        telegram_message_id=888,
    )
    session.add(sent)
    await session.flush()

    changed = await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=uuid.uuid4(),
        now_utc=NOW,
    )
    result = await review_timezone_entry(
        session,
        user.id,
        changed.change.id,
        event.id,
        TimezoneReviewDecision.KEEP_LOCAL,
        now_utc=NOW,
    )

    assert result.applied is True
    assert event.start_at_utc == dt.datetime(2026, 8, 5, 13, 0, tzinfo=dt.UTC)
    assert occurrence.planned_at_utc == dt.datetime(2026, 8, 5, 13, 0, tzinfo=dt.UTC)
    assert event.tz == "Europe/Amsterdam"
    assert sent.fire_at_utc == dt.datetime(2026, 8, 3, 8, 0, tzinfo=dt.UTC)
    assert sent.status is NotificationStatus.SENT
    assert sent.telegram_message_id == 888
    assert not await list_timezone_reviews(session, user.id, changed.change.id)
    assert changed.change.entries_reviewed is True


async def test_task_deadline_keeps_absolute_moment_by_default(session) -> None:
    user = await _user(session)
    task = await _entry(
        session,
        user.id,
        kind=EntryKind.TASK,
        title="Подать документы",
        moment=dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC),
    )
    occurrence = await _occurrence(session, task)

    changed = await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=uuid.uuid4(),
        now_utc=NOW,
    )

    assert task.due_at_utc == dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    assert occurrence.planned_at_utc == dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    await review_timezone_entry(
        session,
        user.id,
        changed.change.id,
        task.id,
        TimezoneReviewDecision.KEEP_ABSOLUTE,
        now_utc=NOW,
    )
    assert task.due_at_utc == dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)


@time_machine.travel("2026-03-28 09:00:00+00:00")
async def test_nonexistent_dst_wall_time_moves_forward_once(session) -> None:
    user = await _user(session)
    routine = await _entry(
        session,
        user.id,
        kind=EntryKind.ROUTINE,
        title="Ночная рутина",
        moment=dt.datetime(2026, 3, 28, 23, 30, tzinfo=dt.UTC),
    )
    routine.local_time = dt.time(2, 30)
    occurrence = await _occurrence(session, routine)

    await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=uuid.uuid4(),
        now_utc=dt.datetime.now(dt.UTC),
    )

    assert occurrence.planned_at_utc == dt.datetime(2026, 3, 29, 1, 30, tzinfo=dt.UTC)
    assert occurrence.planned_at_utc.astimezone(
        dt.timezone(dt.timedelta(hours=2))
    ).time() == dt.time(3, 30)
    assert (
        await session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.entity == "timezone_change")
        )
        == 1
    )
