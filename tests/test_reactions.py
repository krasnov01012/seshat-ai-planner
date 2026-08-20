"""Deterministic Stage 4 notification actions on PostgreSQL."""

from __future__ import annotations

import datetime as dt
import os

import pytest
from sqlalchemy import func, select

from seshat.db.enums import (
    EntryKind,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
    Persistence,
)
from seshat.db.models import (
    ActiveContext,
    AuditLog,
    Entry,
    Notification,
    Occurrence,
    User,
    UserSettings,
)
from seshat.domain.delivery import ReactionContextSource, react_to_message_context
from seshat.domain.reactions import (
    complete_from_notification,
    move_from_notification,
    preview_move_tomorrow,
    skip_from_notification,
    snooze_from_notification,
)
from seshat.domain.scheduling import ReminderDefaults, schedule_notifications
from seshat.domain.users import DomainError

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — reaction actions не проверяются",
)

NOW = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)


async def _seed(session, *, kind: EntryKind = EntryKind.EVENT) -> tuple[int, int, int, int]:
    user = User(telegram_id=92001)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, tz="Europe/Moscow", default_snooze_min=60))
    entry = Entry(
        user_id=user.id,
        kind=kind,
        title="Stage 4 action",
        start_at_utc=None if kind is EntryKind.TASK else NOW,
        due_at_utc=NOW if kind is EntryKind.TASK else None,
        rrule="FREQ=DAILY" if kind is EntryKind.ROUTINE else None,
        tz="Europe/Moscow",
        local_time=dt.time(15, 0),
        persistence=Persistence.IMPORTANT,
    )
    session.add(entry)
    await session.flush()
    occurrence = Occurrence(entry_id=entry.id, user_id=user.id, planned_at_utc=NOW)
    session.add(occurrence)
    await session.flush()
    source = Notification(
        occurrence_id=occurrence.id,
        user_id=user.id,
        fire_at_utc=NOW,
        kind=NotificationKind.MAIN,
        status=NotificationStatus.SENT,
        sent_at_utc=NOW,
        telegram_message_id=700,
    )
    repeat = Notification(
        occurrence_id=occurrence.id,
        user_id=user.id,
        fire_at_utc=NOW + dt.timedelta(minutes=15),
        kind=NotificationKind.REPEAT,
    )
    session.add_all([source, repeat])
    await session.flush()
    session.add(
        ActiveContext(
            user_id=user.id,
            occurrence_id=occurrence.id,
            notification_id=source.id,
            expires_at_utc=NOW + dt.timedelta(hours=3),
        )
    )
    await session.flush()
    return user.id, occurrence.id, source.id, repeat.id


async def test_complete_is_atomic_and_replay_safe(session) -> None:
    user_id, occurrence_id, source_id, repeat_id = await _seed(session)

    first = await complete_from_notification(
        session, user_id, source_id, reacted_at_utc=NOW + dt.timedelta(minutes=1)
    )
    replay = await complete_from_notification(
        session, user_id, source_id, reacted_at_utc=NOW + dt.timedelta(minutes=2)
    )

    occurrence = await session.get(Occurrence, occurrence_id)
    repeat = await session.get(Notification, repeat_id)
    assert first.changed is True and replay.changed is False
    assert occurrence is not None and occurrence.status is OccurrenceStatus.DONE
    assert occurrence.completed_at_utc == NOW + dt.timedelta(minutes=1)
    assert repeat is not None and repeat.status is NotificationStatus.CANCELLED
    assert await session.get(ActiveContext, user_id) is None
    assert (
        await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.entity == "notification_action",
                AuditLog.entity_id == source_id,
            )
        )
        == 1
    )


@pytest.mark.parametrize("kind", list(EntryKind))
async def test_skip_works_for_every_entry_kind(session, kind: EntryKind) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session, kind=kind)

    result = await skip_from_notification(session, user_id, source_id, reacted_at_utc=NOW)

    occurrence = await session.get(Occurrence, occurrence_id)
    assert result.changed is True
    assert occurrence is not None and occurrence.status is OccurrenceStatus.SKIPPED


async def test_snooze_creates_exactly_one_signal_without_moving_occurrence(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session)
    settings = await session.get(UserSettings, user_id)
    assert settings is not None
    settings.default_snooze_min = 20
    reacted_at = NOW + dt.timedelta(minutes=1)

    first = await snooze_from_notification(
        session,
        user_id,
        source_id,
        reacted_at_utc=reacted_at,
        minutes=60,
    )
    replay = await snooze_from_notification(
        session,
        user_id,
        source_id,
        reacted_at_utc=reacted_at + dt.timedelta(minutes=3),
        minutes=60,
    )

    occurrence = await session.get(Occurrence, occurrence_id)
    scheduled = await session.get(Notification, first.scheduled_notification_id)
    assert first.changed is True and replay.changed is False
    assert first.scheduled_notification_id == replay.scheduled_notification_id
    assert occurrence is not None and occurrence.planned_at_utc == NOW
    assert occurrence.moved_count == 0
    assert scheduled is not None
    assert scheduled.fire_at_utc == reacted_at + dt.timedelta(minutes=60)
    assert scheduled.kind is NotificationKind.MAIN


async def test_snooze_from_pre_cancels_original_main(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session)
    source = await session.get(Notification, source_id)
    assert source is not None
    source.kind = NotificationKind.PRE
    source.fire_at_utc = NOW - dt.timedelta(minutes=15)
    original_main = Notification(
        occurrence_id=occurrence_id,
        user_id=user_id,
        fire_at_utc=NOW,
        kind=NotificationKind.MAIN,
    )
    session.add(original_main)
    await session.flush()

    result = await snooze_from_notification(
        session,
        user_id,
        source_id,
        reacted_at_utc=NOW - dt.timedelta(minutes=14),
    )
    await schedule_notifications(
        session,
        now_utc=NOW - dt.timedelta(minutes=14),
        defaults=ReminderDefaults(event_pre_min=(15,)),
        occurrence_ids=(occurrence_id,),
    )

    await session.refresh(original_main)
    scheduled = await session.get(Notification, result.scheduled_notification_id)
    assert original_main.status is NotificationStatus.CANCELLED
    assert scheduled is not None and scheduled.status is NotificationStatus.PENDING
    assert scheduled.fire_at_utc == NOW + dt.timedelta(minutes=46)
    pending = (
        await session.scalars(
            select(Notification).where(
                Notification.occurrence_id == occurrence_id,
                Notification.status == NotificationStatus.PENDING,
            )
        )
    ).all()
    assert [item.id for item in pending] == [scheduled.id]


async def test_move_creates_successor_and_preserves_sent_history(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session)
    target = NOW + dt.timedelta(days=1, hours=2)

    first = await move_from_notification(
        session,
        user_id,
        source_id,
        target,
        reacted_at_utc=NOW + dt.timedelta(minutes=1),
        defaults=ReminderDefaults(event_pre_min=(15,)),
    )
    replay = await move_from_notification(
        session,
        user_id,
        source_id,
        target,
        reacted_at_utc=NOW + dt.timedelta(minutes=2),
        defaults=ReminderDefaults(event_pre_min=(15,)),
    )

    old = await session.get(Occurrence, occurrence_id)
    successor = await session.get(Occurrence, first.successor_occurrence_id)
    source = await session.get(Notification, source_id)
    entry = await session.get(Entry, old.entry_id if old is not None else 0)
    successor_notifications = (
        await session.scalars(
            select(Notification).where(Notification.occurrence_id == first.successor_occurrence_id)
        )
    ).all()
    assert first.changed is True and replay.changed is False
    assert old is not None and old.status is OccurrenceStatus.MOVED and old.moved_count == 1
    assert successor is not None and successor.status is OccurrenceStatus.PENDING
    assert successor.planned_at_utc == target and successor.moved_count == 1
    assert source is not None and source.status is NotificationStatus.SENT
    assert entry is not None and entry.start_at_utc == target
    assert entry.local_time == dt.time(17, 0)
    assert {(item.kind, item.fire_at_utc) for item in successor_notifications} == {
        (NotificationKind.PRE, target - dt.timedelta(minutes=15)),
        (NotificationKind.MAIN, target),
    }


async def test_move_preview_stops_repeats_and_uses_current_timezone(session) -> None:
    user_id, occurrence_id, source_id, repeat_id = await _seed(session)

    preview = await preview_move_tomorrow(session, user_id, source_id, now_utc=NOW)

    occurrence = await session.get(Occurrence, occurrence_id)
    repeat = await session.get(Notification, repeat_id)
    context = await session.get(ActiveContext, user_id)
    assert preview.target_local == dt.datetime(
        2026, 8, 6, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))
    )
    assert occurrence is not None and occurrence.status is OccurrenceStatus.PENDING
    assert occurrence.moved_count == 0
    assert repeat is not None and repeat.status is NotificationStatus.CANCELLED
    assert context is not None and context.notification_id == source_id


async def test_move_preview_avoids_materialized_routine_target(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session, kind=EntryKind.ROUTINE)
    old = await session.get(Occurrence, occurrence_id)
    assert old is not None
    target = old.planned_at_utc + dt.timedelta(days=1)
    successor = Occurrence(
        entry_id=old.entry_id,
        user_id=user_id,
        planned_at_utc=target,
    )
    session.add(successor)
    await session.flush()

    preview = await preview_move_tomorrow(session, user_id, source_id, now_utc=NOW)

    assert preview.target_local == dt.datetime(
        2026, 8, 6, 16, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))
    )
    assert old.status is OccurrenceStatus.PENDING
    assert successor.status is OccurrenceStatus.PENDING


async def test_move_rejects_same_or_colliding_slot_without_mutation(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session, kind=EntryKind.ROUTINE)
    old = await session.get(Occurrence, occurrence_id)
    assert old is not None
    collision = Occurrence(
        entry_id=old.entry_id,
        user_id=user_id,
        planned_at_utc=NOW + dt.timedelta(days=1),
    )
    session.add(collision)
    await session.flush()

    with pytest.raises(DomainError, match="differ"):
        await move_from_notification(
            session,
            user_id,
            source_id,
            NOW,
            reacted_at_utc=NOW - dt.timedelta(seconds=1),
            defaults=ReminderDefaults(),
        )
    with pytest.raises(DomainError, match="already exists"):
        await move_from_notification(
            session,
            user_id,
            source_id,
            collision.planned_at_utc,
            reacted_at_utc=NOW + dt.timedelta(minutes=1),
            defaults=ReminderDefaults(),
        )

    assert old.status is OccurrenceStatus.PENDING
    assert old.moved_count == 0
    assert collision.status is OccurrenceStatus.PENDING


async def test_move_task_updates_canonical_due_anchor(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session, kind=EntryKind.TASK)
    occurrence = await session.get(Occurrence, occurrence_id)
    assert occurrence is not None
    target = NOW + dt.timedelta(days=2, hours=3)

    await move_from_notification(
        session,
        user_id,
        source_id,
        target,
        reacted_at_utc=NOW + dt.timedelta(minutes=1),
        defaults=ReminderDefaults(),
    )

    entry = await session.get(Entry, occurrence.entry_id)
    assert entry is not None
    assert entry.due_at_utc == target
    assert entry.start_at_utc is None
    assert entry.tz == "Europe/Moscow"
    assert entry.local_time == dt.time(18, 0)


async def test_action_rejects_notification_that_was_not_sent(session) -> None:
    user_id, _occurrence_id, _source_id, repeat_id = await _seed(session)

    with pytest.raises(DomainError, match="notification has not been sent"):
        await complete_from_notification(session, user_id, repeat_id, reacted_at_utc=NOW)


async def test_in_flight_notification_with_active_context_accepts_fast_click(session) -> None:
    user_id, occurrence_id, source_id, _ = await _seed(session)
    source = await session.get(Notification, source_id)
    assert source is not None
    source.status = NotificationStatus.PENDING
    source.sent_at_utc = None
    source.telegram_message_id = None
    source.next_attempt_at_utc = NOW + dt.timedelta(minutes=2)

    result = await complete_from_notification(
        session,
        user_id,
        source_id,
        reacted_at_utc=NOW + dt.timedelta(seconds=1),
    )

    occurrence = await session.get(Occurrence, occurrence_id)
    assert result.changed is True
    assert occurrence is not None and occurrence.status is OccurrenceStatus.DONE


async def test_reply_context_beats_newer_active_context_and_unknown_does_not_fallback(
    session,
) -> None:
    user_id, first_occurrence_id, first_source_id, first_repeat_id = await _seed(session)
    first = await session.get(Occurrence, first_occurrence_id)
    assert first is not None
    second_entry = Entry(
        user_id=user_id,
        kind=EntryKind.EVENT,
        title="Newer reminder",
        start_at_utc=NOW + dt.timedelta(hours=1),
        tz="Europe/Moscow",
        local_time=dt.time(16, 0),
        persistence=Persistence.IMPORTANT,
    )
    session.add(second_entry)
    await session.flush()
    second = Occurrence(
        entry_id=second_entry.id,
        user_id=user_id,
        planned_at_utc=NOW + dt.timedelta(hours=1),
    )
    session.add(second)
    await session.flush()
    second_source = Notification(
        occurrence_id=second.id,
        user_id=user_id,
        fire_at_utc=NOW,
        kind=NotificationKind.MAIN,
        status=NotificationStatus.SENT,
        sent_at_utc=NOW,
        telegram_message_id=701,
    )
    second_repeat = Notification(
        occurrence_id=second.id,
        user_id=user_id,
        fire_at_utc=NOW + dt.timedelta(minutes=15),
        kind=NotificationKind.REPEAT,
    )
    session.add_all([second_source, second_repeat])
    await session.flush()
    context = await session.get(ActiveContext, user_id)
    assert context is not None
    context.occurrence_id = second.id
    context.notification_id = second_source.id
    context.expires_at_utc = NOW + dt.timedelta(hours=3)
    await session.flush()

    unknown = await react_to_message_context(
        session,
        user_id,
        reply_telegram_message_id=999_999,
        reacted_at_utc=NOW + dt.timedelta(minutes=1),
    )
    explicit = await react_to_message_context(
        session,
        user_id,
        reply_telegram_message_id=700,
        reacted_at_utc=NOW + dt.timedelta(minutes=2),
    )

    first_repeat = await session.get(Notification, first_repeat_id)
    await session.refresh(second_repeat)
    await session.refresh(context)
    assert unknown.reacted is False
    assert explicit.source is ReactionContextSource.REPLY
    assert explicit.occurrence_id == first.id
    assert explicit.notification_id == first_source_id
    assert first_repeat is not None and first_repeat.status is NotificationStatus.CANCELLED
    assert second_repeat.status is NotificationStatus.PENDING
    assert context.occurrence_id == second.id

    active = await react_to_message_context(
        session,
        user_id,
        reply_telegram_message_id=None,
        reacted_at_utc=NOW + dt.timedelta(minutes=3),
    )
    await session.refresh(second_repeat)
    assert active.source is ReactionContextSource.ACTIVE
    assert active.occurrence_id == second.id
    assert active.notification_id == second_source.id
    assert second_repeat.status is NotificationStatus.CANCELLED
    assert await session.get(ActiveContext, user_id) is None
