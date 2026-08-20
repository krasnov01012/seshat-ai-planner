"""Durable morning digest on real PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import delete, func, select

from seshat.db.base import make_engine, make_session_factory
from seshat.db.enums import EntryKind, NotificationKind, NotificationStatus
from seshat.db.models import AuditLog, Entry, Notification, Occurrence, User, UserSettings
from seshat.domain.delivery import DeliveryReceipt, PermanentDeliveryError, TransientDeliveryError
from seshat.domain.digests import (
    MorningDigestCommand,
    deliver_due_morning_digests,
    digest_due_at_utc,
)
from seshat.domain.timezones import confirm_timezone_change

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)

NOW = dt.datetime(2026, 8, 5, 5, 30, tzinfo=dt.UTC)


@dataclass
class RecordingDigestTransport:
    outcomes: list[DeliveryReceipt | Exception] = field(default_factory=list)
    commands: list[MorningDigestCommand] = field(default_factory=list)

    async def send_digest(self, command: MorningDigestCommand) -> DeliveryReceipt:
        self.commands.append(command)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return DeliveryReceipt(message_id=7000 + len(self.commands))


async def _seed_silent_night(
    session,
    *,
    telegram_id: int = 81001,
    digest_enabled: bool = False,
    digest_time: dt.time = dt.time(8, 30),
    sent_at: dt.datetime = dt.datetime(2026, 8, 5, 0, 10, tzinfo=dt.UTC),
) -> tuple[int, int]:
    user = User(telegram_id=telegram_id)
    session.add(user)
    await session.flush()
    session.add(
        UserSettings(
            user_id=user.id,
            tz="Europe/Moscow",
            digest_enabled=digest_enabled,
            digest_time=digest_time,
        )
    )
    entry = Entry(
        user_id=user.id,
        kind=EntryKind.EVENT,
        title="Ночной поезд <важный>",
        start_at_utc=sent_at,
        tz="Europe/Moscow",
        local_time=dt.time(3, 10),
    )
    session.add(entry)
    await session.flush()
    occurrence = Occurrence(
        entry_id=entry.id,
        user_id=user.id,
        planned_at_utc=entry.start_at_utc,
    )
    session.add(occurrence)
    await session.flush()
    notification = Notification(
        occurrence_id=occurrence.id,
        user_id=user.id,
        fire_at_utc=entry.start_at_utc,
        kind=NotificationKind.MAIN,
        status=NotificationStatus.SENT,
        silent=True,
        sent_at_utc=sent_at,
        telegram_message_id=99,
    )
    session.add(notification)
    await session.flush()
    return user.id, notification.id


async def test_night_safety_recap_is_sent_once_even_when_digest_disabled(
    session, session_factory
) -> None:
    user_id, notification_id = await _seed_silent_night(session)
    await session.commit()
    transport = RecordingDigestTransport()

    first = await deliver_due_morning_digests(session_factory, transport, now_utc=NOW)
    second = await deliver_due_morning_digests(session_factory, transport, now_utc=NOW)

    assert first.sent == 1
    assert second.sent == 0
    assert len(transport.commands) == 1
    day = transport.commands[0].day
    assert [item.title for item in day.night] == ["Ночной поезд <важный>"]
    assert day.items == () and day.missed == ()
    async with session_factory() as check:
        notification = await check.get(Notification, notification_id)
        assert notification is not None and notification.digest_included_at_utc == NOW
        assert (
            await check.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.user_id == user_id,
                    AuditLog.entity == "morning_digest",
                )
            )
            == 1
        )


async def test_transient_digest_failure_leaves_notification_for_retry(
    session, session_factory
) -> None:
    _, notification_id = await _seed_silent_night(session)
    await session.commit()
    transport = RecordingDigestTransport(outcomes=[TransientDeliveryError("telegram_unavailable")])

    failed = await deliver_due_morning_digests(session_factory, transport, now_utc=NOW)
    too_early = await deliver_due_morning_digests(session_factory, transport, now_utc=NOW)
    retried = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=NOW + dt.timedelta(minutes=5),
    )

    assert failed.retried == 1
    assert too_early.sent == 0
    assert retried.sent == 1
    async with session_factory() as check:
        notification = await check.get(Notification, notification_id)
        assert notification is not None
        assert notification.digest_included_at_utc == NOW + dt.timedelta(minutes=5)
        assert notification.digest_attempt_count == 1
        assert notification.digest_next_attempt_at_utc is None


async def test_permanent_digest_failure_does_not_mark_unsent_items_included(
    session, session_factory
) -> None:
    _, notification_id = await _seed_silent_night(session)
    await session.commit()
    transport = RecordingDigestTransport(outcomes=[PermanentDeliveryError("telegram_rejected")])

    failed = await deliver_due_morning_digests(session_factory, transport, now_utc=NOW)
    too_early = await deliver_due_morning_digests(session_factory, transport, now_utc=NOW)
    retried = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=NOW + dt.timedelta(hours=6),
    )

    assert failed.failed == 1
    assert too_early.sent == 0
    assert retried.sent == 1
    assert len(transport.commands) == 2
    async with session_factory() as check:
        notification = await check.get(Notification, notification_id)
        assert notification is not None
        assert notification.digest_included_at_utc == NOW + dt.timedelta(hours=6)
        assert notification.digest_attempt_count == 1
        assert notification.digest_next_attempt_at_utc is None


async def test_digest_due_time_follows_confirmed_timezone(session) -> None:
    user = User(telegram_id=81002)
    session.add(user)
    await session.flush()
    settings = UserSettings(
        user_id=user.id,
        tz="Europe/Moscow",
        digest_enabled=True,
        digest_time=dt.time(8, 30),
    )
    session.add(settings)
    await session.flush()

    assert digest_due_at_utc(settings, dt.date(2026, 8, 5)) == dt.datetime(
        2026, 8, 5, 5, 30, tzinfo=dt.UTC
    )

    await confirm_timezone_change(
        session,
        user.id,
        "Europe/Amsterdam",
        expected_tz_from="Europe/Moscow",
        confirmation_id=uuid.uuid4(),
        now_utc=dt.datetime(2026, 8, 3, 9, tzinfo=dt.UTC),
    )

    assert settings.digest_time == dt.time(8, 30)
    assert digest_due_at_utc(settings, dt.date(2026, 8, 5)) == dt.datetime(
        2026, 8, 5, 6, 30, tzinfo=dt.UTC
    )


async def test_digest_time_inside_quiet_hours_is_deferred(session) -> None:
    user = User(telegram_id=81003)
    session.add(user)
    await session.flush()
    settings = UserSettings(
        user_id=user.id,
        tz="Europe/Amsterdam",
        quiet_from=dt.time(23, 0),
        quiet_to=dt.time(8, 0),
        digest_time=dt.time(7, 30),
    )
    session.add(settings)
    await session.flush()

    assert digest_due_at_utc(settings, dt.date(2026, 3, 29)) == dt.datetime(
        2026, 3, 29, 6, 0, tzinfo=dt.UTC
    )


async def test_evening_silent_waits_for_next_morning(session, session_factory) -> None:
    night = dt.datetime(2026, 8, 5, 20, 30, tzinfo=dt.UTC)  # 23:30 Moscow
    _, notification_id = await _seed_silent_night(session, sent_at=night)
    await session.commit()
    transport = RecordingDigestTransport()

    at_night = await deliver_due_morning_digests(session_factory, transport, now_utc=night)
    in_morning = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=dt.datetime(2026, 8, 6, 5, 30, tzinfo=dt.UTC),
    )

    assert at_night.sent == 0
    assert in_morning.sent == 1
    assert len(transport.commands) == 1
    async with session_factory() as check:
        notification = await check.get(Notification, notification_id)
        assert notification is not None and notification.digest_included_at_utc is not None


async def test_evening_digest_time_inside_quiet_is_due_at_next_quiet_end(
    session, session_factory
) -> None:
    sent_at = dt.datetime(2026, 8, 5, 20, 0, tzinfo=dt.UTC)
    await _seed_silent_night(
        session,
        digest_time=dt.time(23, 30),
        sent_at=sent_at,
    )
    await session.commit()
    transport = RecordingDigestTransport()

    before_end = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=dt.datetime(2026, 8, 6, 4, 59, tzinfo=dt.UTC),
    )
    at_end = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=dt.datetime(2026, 8, 6, 5, 0, tzinfo=dt.UTC),
    )

    assert before_end.sent == 0
    assert at_end.sent == 1


async def test_digest_batch_limit_does_not_starve_later_users(session, session_factory) -> None:
    await _seed_silent_night(session, telegram_id=81011)
    await _seed_silent_night(session, telegram_id=81012)
    await session.commit()
    transport = RecordingDigestTransport()

    first = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=NOW,
        batch_size=1,
    )
    second = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=NOW,
        batch_size=1,
    )

    assert first.sent == 1
    assert second.sent == 1
    assert {command.telegram_id for command in transport.commands} == {81011, 81012}


async def test_failed_early_user_does_not_starve_later_user(session, session_factory) -> None:
    await _seed_silent_night(session, telegram_id=81021)
    await _seed_silent_night(session, telegram_id=81022)
    await session.commit()
    transport = RecordingDigestTransport(outcomes=[TransientDeliveryError("telegram_unavailable")])

    first = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=NOW,
        batch_size=1,
    )
    second = await deliver_due_morning_digests(
        session_factory,
        transport,
        now_utc=NOW,
        batch_size=1,
    )

    assert first.retried == 1
    assert second.sent == 1
    assert [command.telegram_id for command in transport.commands] == [81021, 81022]


async def test_parallel_digest_ticks_send_one_message(schema) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    first_engine = make_engine(database_url)
    second_engine = make_engine(database_url)
    first_factory = make_session_factory(first_engine)
    second_factory = make_session_factory(second_engine)
    user_id: int | None = None
    try:
        async with first_factory() as session, session.begin():
            user_id, _ = await _seed_silent_night(session)
        transport = RecordingDigestTransport()

        results = await asyncio.gather(
            deliver_due_morning_digests(first_factory, transport, now_utc=NOW),
            deliver_due_morning_digests(second_factory, transport, now_utc=NOW),
        )

        assert sum(result.sent for result in results) == 1
        assert len(transport.commands) == 1
    finally:
        if user_id is not None:
            async with first_factory() as session, session.begin():
                await session.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await session.execute(delete(User).where(User.id == user_id))
        await first_engine.dispose()
        await second_engine.dispose()
