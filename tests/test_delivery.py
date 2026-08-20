"""Delivery tests use PostgreSQL because SKIP LOCKED is the core guarantee."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
import time_machine
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from seshat.db.base import make_engine, make_session_factory
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
from seshat.domain.delivery import (
    DeliveryCommand,
    DeliveryReceipt,
    PermanentDeliveryError,
    RepeatPolicy,
    RetryPolicy,
    TransientDeliveryError,
    acknowledge_occurrence,
    apply_quiet_policy,
    deliver_due,
    list_night_deliveries,
    quiet_window,
    react_to_active_context,
)
from seshat.domain.locks import lock_user_context

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)


@dataclass(frozen=True, slots=True)
class Seeded:
    user_id: int
    occurrence_id: int
    notification_id: int


@dataclass(slots=True)
class DeliveryDatabase:
    factory: async_sessionmaker[AsyncSession]
    user_ids: list[int] = field(default_factory=list)

    async def seed(
        self,
        *,
        fire_at: dt.datetime,
        occurrence_at: dt.datetime | None = None,
        notification_kind: NotificationKind = NotificationKind.MAIN,
        persistence: Persistence = Persistence.NORMAL,
        tz: str = "Europe/Moscow",
        quiet_from: dt.time = dt.time(23, 0),
        quiet_to: dt.time = dt.time(8, 0),
    ) -> Seeded:
        occurrence_at = occurrence_at or fire_at
        async with self.factory() as session, session.begin():
            telegram_id = uuid.uuid4().int & ((1 << 63) - 1)
            user = User(telegram_id=telegram_id)
            session.add(user)
            await session.flush()
            self.user_ids.append(user.id)
            session.add(
                UserSettings(
                    user_id=user.id,
                    tz=tz,
                    quiet_from=quiet_from,
                    quiet_to=quiet_to,
                )
            )
            entry = Entry(
                user_id=user.id,
                kind=EntryKind.EVENT,
                title="Собеседование",
                start_at_utc=occurrence_at,
                tz=tz,
                local_time=occurrence_at.astimezone(dt.UTC).time(),
                persistence=persistence,
            )
            session.add(entry)
            await session.flush()
            occurrence = Occurrence(
                entry_id=entry.id,
                user_id=user.id,
                planned_at_utc=occurrence_at,
            )
            session.add(occurrence)
            await session.flush()
            notification = Notification(
                occurrence_id=occurrence.id,
                user_id=user.id,
                fire_at_utc=fire_at,
                kind=notification_kind,
            )
            session.add(notification)
            await session.flush()
            return Seeded(user.id, occurrence.id, notification.id)


@pytest_asyncio.fixture
async def delivery_db(engine: AsyncEngine) -> AsyncIterator[DeliveryDatabase]:
    database = DeliveryDatabase(make_session_factory(engine))
    try:
        yield database
    finally:
        if database.user_ids:
            async with database.factory() as session, session.begin():
                await session.execute(
                    delete(AuditLog).where(AuditLog.user_id.in_(database.user_ids))
                )
                await session.execute(delete(User).where(User.id.in_(database.user_ids)))


@dataclass(slots=True)
class RecordingTransport:
    outcomes: list[DeliveryReceipt | Exception] = field(default_factory=list)
    commands: list[DeliveryCommand] = field(default_factory=list)

    async def send(self, command: DeliveryCommand) -> DeliveryReceipt:
        self.commands.append(command)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return DeliveryReceipt(message_id=1000 + len(self.commands))


async def _notification(
    factory: async_sessionmaker[AsyncSession], notification_id: int
) -> Notification:
    async with factory() as session:
        return (
            await session.execute(select(Notification).where(Notification.id == notification_id))
        ).scalar_one()


@time_machine.travel("2026-08-05 12:00:00+00:00")
async def test_late_delivery_is_sent_once_and_survives_restart(
    delivery_db: DeliveryDatabase,
) -> None:
    seeded = await delivery_db.seed(
        fire_at=dt.datetime(2026, 8, 5, 11, 45, tzinfo=dt.UTC),
        occurrence_at=dt.datetime(2026, 8, 5, 13, 0, tzinfo=dt.UTC),
    )
    transport = RecordingTransport()

    first = await deliver_due(delivery_db.factory, transport)
    second = await deliver_due(delivery_db.factory, transport)

    assert first.sent == 1
    assert second.sent == 0
    assert len(transport.commands) == 1
    assert transport.commands[0].late is True
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.status is NotificationStatus.SENT
    assert stored.telegram_message_id == 1001


async def test_exact_late_threshold_is_delivered_not_missed(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now - dt.timedelta(minutes=30),
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport()

    result = await deliver_due(delivery_db.factory, transport, now_utc=now)

    assert result.sent == 1
    assert result.missed == 0
    assert transport.commands[0].late is True
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.status is NotificationStatus.SENT


async def test_old_pre_is_missed_without_marking_occurrence_missed(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now - dt.timedelta(minutes=31),
        occurrence_at=now + dt.timedelta(hours=1),
        notification_kind=NotificationKind.PRE,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport()

    result = await deliver_due(delivery_db.factory, transport, now_utc=now)

    assert result.missed == 1
    assert not transport.commands
    async with delivery_db.factory() as session:
        notification = await session.get(Notification, seeded.notification_id)
        occurrence = await session.get(Occurrence, seeded.occurrence_id)
        assert notification is not None and notification.status is NotificationStatus.MISSED
        assert occurrence is not None and occurrence.status is OccurrenceStatus.PENDING


async def test_missed_main_marks_occurrence_missed(delivery_db: DeliveryDatabase) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now - dt.timedelta(minutes=31),
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )

    result = await deliver_due(delivery_db.factory, RecordingTransport(), now_utc=now)

    assert result.missed == 1
    async with delivery_db.factory() as session:
        occurrence = await session.get(Occurrence, seeded.occurrence_id)
        assert occurrence is not None and occurrence.status is OccurrenceStatus.MISSED


@time_machine.travel("2026-08-05 00:00:00+00:00")
async def test_main_at_three_local_is_silent_and_appears_in_night_digest(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime.now(dt.UTC)
    seeded = await delivery_db.seed(fire_at=now)
    transport = RecordingTransport()

    result = await deliver_due(delivery_db.factory, transport)

    assert result.sent == 1
    assert transport.commands[0].silent is True
    async with delivery_db.factory() as session:
        night = await list_night_deliveries(
            session,
            seeded.user_id,
            window_start_utc=now - dt.timedelta(hours=1),
            window_end_utc=now + dt.timedelta(hours=1),
        )
    assert [item.notification_id for item in night] == [seeded.notification_id]


async def test_pre_in_quiet_hours_moves_to_end_only_before_occurrence(
    delivery_db: DeliveryDatabase,
) -> None:
    three_local = dt.datetime(2026, 8, 5, 0, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=three_local,
        occurrence_at=dt.datetime(2026, 8, 5, 7, 0, tzinfo=dt.UTC),
        notification_kind=NotificationKind.PRE,
    )
    transport = RecordingTransport()

    first = await deliver_due(delivery_db.factory, transport, now_utc=three_local)
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    second = await deliver_due(delivery_db.factory, transport, now_utc=stored.fire_at_utc)

    assert first.rescheduled == 1
    assert stored.fire_at_utc == dt.datetime(2026, 8, 5, 5, 0, tzinfo=dt.UTC)
    assert second.sent == 1
    assert transport.commands[0].silent is False


async def test_pre_in_quiet_hours_is_silent_when_quiet_end_is_too_late(
    delivery_db: DeliveryDatabase,
) -> None:
    three_local = dt.datetime(2026, 8, 5, 0, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=three_local,
        occurrence_at=dt.datetime(2026, 8, 5, 4, 0, tzinfo=dt.UTC),
        notification_kind=NotificationKind.PRE,
    )
    transport = RecordingTransport()

    result = await deliver_due(delivery_db.factory, transport, now_utc=three_local)

    assert result.sent == 1
    assert transport.commands[0].silent is True
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.fire_at_utc == three_local


async def test_repeat_is_not_sent_during_quiet_hours(
    delivery_db: DeliveryDatabase,
) -> None:
    three_local = dt.datetime(2026, 8, 5, 0, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=three_local,
        occurrence_at=three_local - dt.timedelta(minutes=15),
        notification_kind=NotificationKind.REPEAT,
    )
    transport = RecordingTransport()

    night = await deliver_due(delivery_db.factory, transport, now_utc=three_local)
    assert not transport.commands
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    morning = await deliver_due(delivery_db.factory, transport, now_utc=stored.fire_at_utc)

    assert night.rescheduled == 1
    assert stored.fire_at_utc == dt.datetime(2026, 8, 5, 5, 0, tzinfo=dt.UTC)
    assert morning.sent == 1
    assert transport.commands[0].silent is False


async def test_pre_due_before_quiet_is_not_moved_by_a_late_tick(
    delivery_db: DeliveryDatabase,
) -> None:
    fire_at = dt.datetime(2026, 8, 5, 19, 59, tzinfo=dt.UTC)  # 22:59 Moscow
    now = fire_at + dt.timedelta(minutes=2)  # tick reached it at 23:01 Moscow
    seeded = await delivery_db.seed(
        fire_at=fire_at,
        occurrence_at=dt.datetime(2026, 8, 6, 7, 0, tzinfo=dt.UTC),
        notification_kind=NotificationKind.PRE,
    )
    transport = RecordingTransport()

    result = await deliver_due(delivery_db.factory, transport, now_utc=now)

    assert result.sent == 1
    assert transport.commands[0].silent is True
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.fire_at_utc == fire_at


async def test_transient_retry_is_persisted_then_succeeds(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport(
        outcomes=[TransientDeliveryError("telegram.retry_after", retry_after_s=60)]
    )

    first = await deliver_due(delivery_db.factory, transport, now_utc=now)
    too_early = await deliver_due(
        delivery_db.factory, transport, now_utc=now + dt.timedelta(seconds=59)
    )
    second = await deliver_due(
        delivery_db.factory, transport, now_utc=now + dt.timedelta(seconds=60)
    )

    assert first.retried == 1
    assert too_early.sent == 0
    assert second.sent == 1
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.attempt_count == 1
    assert stored.last_error_code is None


async def test_permanent_error_is_not_hot_looped(delivery_db: DeliveryDatabase) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport(outcomes=[PermanentDeliveryError("telegram.forbidden")])

    first = await deliver_due(delivery_db.factory, transport, now_utc=now)
    second = await deliver_due(delivery_db.factory, transport, now_utc=now)

    assert first.failed == 1
    assert second.failed == 0
    assert len(transport.commands) == 1
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.status is NotificationStatus.FAILED
    assert stored.last_error_code == "telegram.forbidden"


async def test_important_chain_has_three_repeats_and_can_be_cancelled(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport()
    repeat_policy = RepeatPolicy(interval_min=15, max_repeats=3)

    await deliver_due(delivery_db.factory, transport, now_utc=now, repeat_policy=repeat_policy)
    for number in range(1, 4):
        await deliver_due(
            delivery_db.factory,
            transport,
            now_utc=now + dt.timedelta(minutes=15 * number),
            repeat_policy=repeat_policy,
        )

    assert len(transport.commands) == 4
    async with delivery_db.factory() as session:
        repeats = (
            await session.execute(
                select(Notification).where(
                    Notification.occurrence_id == seeded.occurrence_id,
                    Notification.kind == NotificationKind.REPEAT,
                )
            )
        ).scalars()
        assert len(list(repeats)) == 3

    second = await delivery_db.seed(
        fire_at=now,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    await deliver_due(delivery_db.factory, transport, now_utc=now, batch_size=1)
    async with delivery_db.factory() as session, session.begin():
        reaction = await acknowledge_occurrence(
            session,
            second.user_id,
            second.occurrence_id,
            reacted_at_utc=now + dt.timedelta(minutes=1),
        )
    assert reaction.cancelled == 1


async def test_any_active_context_reaction_stops_important_repeats(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport()
    await deliver_due(delivery_db.factory, transport, now_utc=now)

    async with delivery_db.factory() as session, session.begin():
        reaction = await react_to_active_context(
            session,
            seeded.user_id,
            reacted_at_utc=now + dt.timedelta(minutes=1),
        )

    later = await deliver_due(
        delivery_db.factory,
        transport,
        now_utc=now + dt.timedelta(hours=1),
    )
    assert reaction.reacted is True
    assert reaction.occurrence_id == seeded.occurrence_id
    assert reaction.cancelled == 1
    assert later.sent == 0
    assert len(transport.commands) == 1

    async with delivery_db.factory() as session, session.begin():
        replay = await react_to_active_context(
            session,
            seeded.user_id,
            reacted_at_utc=now + dt.timedelta(minutes=2),
        )
    assert replay.reacted is False


async def test_pre_reaction_durably_prevents_repeats_after_main(
    delivery_db: DeliveryDatabase,
) -> None:
    pre_at = dt.datetime(2026, 8, 5, 11, 0, tzinfo=dt.UTC)
    main_at = pre_at + dt.timedelta(hours=1)
    seeded = await delivery_db.seed(
        fire_at=pre_at,
        occurrence_at=main_at,
        notification_kind=NotificationKind.PRE,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    async with delivery_db.factory() as session, session.begin():
        session.add(
            Notification(
                occurrence_id=seeded.occurrence_id,
                user_id=seeded.user_id,
                fire_at_utc=main_at,
                kind=NotificationKind.MAIN,
            )
        )
    transport = RecordingTransport()

    await deliver_due(delivery_db.factory, transport, now_utc=pre_at)
    async with delivery_db.factory() as session, session.begin():
        reaction = await acknowledge_occurrence(
            session,
            seeded.user_id,
            seeded.occurrence_id,
            reacted_at_utc=pre_at + dt.timedelta(minutes=1),
        )
    await deliver_due(delivery_db.factory, transport, now_utc=main_at)
    await deliver_due(
        delivery_db.factory,
        transport,
        now_utc=main_at + dt.timedelta(hours=1),
    )

    assert reaction.cancelled == 0
    assert [command.notification_kind for command in transport.commands] == [
        NotificationKind.PRE,
        NotificationKind.MAIN,
    ]
    async with delivery_db.factory() as session:
        repeats = await session.scalar(
            select(Notification.id).where(
                Notification.occurrence_id == seeded.occurrence_id,
                Notification.kind == NotificationKind.REPEAT,
            )
        )
        marker_count = len(
            (
                await session.scalars(
                    select(AuditLog.id).where(
                        AuditLog.entity == "occurrence_reaction",
                        AuditLog.entity_id == seeded.occurrence_id,
                    )
                )
            ).all()
        )
    assert repeats is None
    assert marker_count == 1


async def test_normal_main_never_creates_repeat(delivery_db: DeliveryDatabase) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        persistence=Persistence.NORMAL,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport()

    await deliver_due(delivery_db.factory, transport, now_utc=now)
    await deliver_due(
        delivery_db.factory,
        transport,
        now_utc=now + dt.timedelta(hours=1),
    )

    assert len(transport.commands) == 1
    async with delivery_db.factory() as session:
        repeat = await session.scalar(
            select(Notification.id).where(
                Notification.occurrence_id == seeded.occurrence_id,
                Notification.kind == NotificationKind.REPEAT,
            )
        )
    assert repeat is None


async def test_reaction_while_send_is_in_flight_is_not_lost(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = BlockingTransport()
    reaction_engine = make_engine(os.environ["TEST_DATABASE_URL"])
    reaction_factory = make_session_factory(reaction_engine)
    delivery_task = asyncio.create_task(
        deliver_due(delivery_db.factory, transport, now_utc=now, batch_size=1)
    )
    try:
        await asyncio.wait_for(transport.entered.wait(), timeout=5)
        async with reaction_factory() as session, session.begin():
            reaction = await asyncio.wait_for(
                react_to_active_context(
                    session,
                    seeded.user_id,
                    reacted_at_utc=now + dt.timedelta(seconds=1),
                ),
                timeout=5,
            )
        transport.release.set()
        delivered = await asyncio.wait_for(delivery_task, timeout=5)
    finally:
        transport.release.set()
        await reaction_engine.dispose()

    later = await deliver_due(
        delivery_db.factory,
        RecordingTransport(),
        now_utc=now + dt.timedelta(hours=1),
    )
    assert reaction.reacted is True
    assert delivered.sent == 1
    assert later.sent == 0


async def test_due_repeat_and_ack_use_one_lock_order(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        notification_kind=NotificationKind.REPEAT,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    reaction_engine = make_engine(os.environ["TEST_DATABASE_URL"])
    reaction_factory = make_session_factory(reaction_engine)
    transport = RecordingTransport()

    async def react() -> None:
        async with reaction_factory() as session, session.begin():
            await acknowledge_occurrence(
                session,
                seeded.user_id,
                seeded.occurrence_id,
                reacted_at_utc=now,
            )

    try:
        await asyncio.wait_for(
            asyncio.gather(
                deliver_due(delivery_db.factory, transport, now_utc=now, batch_size=1),
                react(),
            ),
            timeout=5,
        )
    finally:
        await reaction_engine.dispose()

    async with delivery_db.factory() as session:
        pending_repeats = (
            await session.scalars(
                select(Notification.id).where(
                    Notification.occurrence_id == seeded.occurrence_id,
                    Notification.kind == NotificationKind.REPEAT,
                    Notification.status == NotificationStatus.PENDING,
                )
            )
        ).all()
        markers = (
            await session.scalars(
                select(AuditLog.id).where(
                    AuditLog.entity == "occurrence_reaction",
                    AuditLog.entity_id == seeded.occurrence_id,
                )
            )
        ).all()
    assert not pending_repeats
    assert len(markers) == 1
    assert len(transport.commands) <= 1


async def test_context_replacement_waits_for_generic_reaction(
    delivery_db: DeliveryDatabase,
) -> None:
    first_at = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    first = await delivery_db.seed(
        fire_at=first_at,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    await deliver_due(delivery_db.factory, RecordingTransport(), now_utc=first_at)
    second_at = first_at + dt.timedelta(minutes=1)
    async with delivery_db.factory() as session, session.begin():
        entry = Entry(
            user_id=first.user_id,
            kind=EntryKind.EVENT,
            title="Второе напоминание",
            start_at_utc=second_at,
            tz="Europe/Moscow",
            local_time=dt.time(15, 1),
            persistence=Persistence.IMPORTANT,
        )
        session.add(entry)
        await session.flush()
        occurrence = Occurrence(
            entry_id=entry.id,
            user_id=first.user_id,
            planned_at_utc=second_at,
        )
        session.add(occurrence)
        await session.flush()
        second_occurrence_id = occurrence.id
        session.add(
            Notification(
                occurrence_id=occurrence.id,
                user_id=first.user_id,
                fire_at_utc=second_at,
                kind=NotificationKind.MAIN,
            )
        )

    reaction_engine = make_engine(os.environ["TEST_DATABASE_URL"])
    reaction_factory = make_session_factory(reaction_engine)
    transport = BlockingTransport()
    delivery_task: asyncio.Task[object] | None = None
    try:
        async with reaction_factory() as session, session.begin():
            await lock_user_context(session, first.user_id)
            delivery_task = asyncio.create_task(
                deliver_due(
                    delivery_db.factory,
                    transport,
                    now_utc=second_at,
                    batch_size=1,
                )
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(transport.entered.wait(), timeout=0.1)
            reaction = await react_to_active_context(
                session,
                first.user_id,
                reacted_at_utc=second_at,
            )
        await asyncio.wait_for(transport.entered.wait(), timeout=5)
        transport.release.set()
        assert delivery_task is not None
        await asyncio.wait_for(delivery_task, timeout=5)
    finally:
        transport.release.set()
        if delivery_task is not None and not delivery_task.done():
            await delivery_task
        await reaction_engine.dispose()

    assert reaction.reacted is True
    assert reaction.occurrence_id == first.occurrence_id
    assert transport.calls == 1
    async with delivery_db.factory() as session:
        context = await session.get(ActiveContext, first.user_id)
    assert context is not None and context.occurrence_id == second_occurrence_id


async def test_ack_replay_has_one_durable_marker(delivery_db: DeliveryDatabase) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        persistence=Persistence.IMPORTANT,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    await deliver_due(delivery_db.factory, RecordingTransport(), now_utc=now)

    async with delivery_db.factory() as session, session.begin():
        await acknowledge_occurrence(
            session,
            seeded.user_id,
            seeded.occurrence_id,
            reacted_at_utc=now + dt.timedelta(minutes=1),
        )
        await acknowledge_occurrence(
            session,
            seeded.user_id,
            seeded.occurrence_id,
            reacted_at_utc=now + dt.timedelta(minutes=2),
        )
    async with delivery_db.factory() as session:
        markers = (
            await session.scalars(
                select(AuditLog.id).where(
                    AuditLog.entity == "occurrence_reaction",
                    AuditLog.entity_id == seeded.occurrence_id,
                )
            )
        ).all()
    assert len(markers) == 1


async def test_active_context_expires_at_exact_ttl_boundary(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    await deliver_due(
        delivery_db.factory,
        RecordingTransport(),
        now_utc=now,
        active_context_ttl_min=180,
    )

    async with delivery_db.factory() as session, session.begin():
        reaction = await react_to_active_context(
            session,
            seeded.user_id,
            reacted_at_utc=now + dt.timedelta(minutes=180),
        )
    assert reaction.reacted is False


async def test_quiet_end_resolves_dst_gap_and_fold_without_waking() -> None:
    spring = quiet_window(
        dt.datetime(2026, 3, 29, 0, 30, tzinfo=dt.UTC),
        tz="Europe/Amsterdam",
        quiet_from=dt.time(23, 0),
        quiet_to=dt.time(2, 30),
    )
    autumn = quiet_window(
        dt.datetime(2026, 10, 25, 0, 15, tzinfo=dt.UTC),
        tz="Europe/Amsterdam",
        quiet_from=dt.time(23, 0),
        quiet_to=dt.time(2, 30),
    )

    assert spring.end_at_utc == dt.datetime(2026, 3, 29, 1, 0, tzinfo=dt.UTC)
    assert autumn.end_at_utc == dt.datetime(2026, 10, 25, 1, 30, tzinfo=dt.UTC)
    repeat = apply_quiet_policy(
        NotificationKind.REPEAT,
        dt.datetime(2026, 10, 25, 0, 15, tzinfo=dt.UTC),
        dt.datetime(2026, 10, 25, 8, 0, tzinfo=dt.UTC),
        tz="Europe/Amsterdam",
        quiet_from=dt.time(23, 0),
        quiet_to=dt.time(2, 30),
    )
    assert repeat.fire_at_utc == autumn.end_at_utc
    assert repeat.silent is False


@dataclass(slots=True)
class BlockingTransport:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def send(self, command: DeliveryCommand) -> DeliveryReceipt:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return DeliveryReceipt(message_id=42)


async def test_two_independent_workers_do_not_send_same_row(
    delivery_db: DeliveryDatabase,
) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    await delivery_db.seed(
        fire_at=now,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    first_engine = make_engine(os.environ["TEST_DATABASE_URL"])
    second_engine = make_engine(os.environ["TEST_DATABASE_URL"])
    transport = BlockingTransport()
    try:
        first_task = asyncio.create_task(
            deliver_due(make_session_factory(first_engine), transport, now_utc=now, batch_size=1)
        )
        await asyncio.wait_for(transport.entered.wait(), timeout=5)
        second = await asyncio.wait_for(
            deliver_due(make_session_factory(second_engine), transport, now_utc=now, batch_size=1),
            timeout=5,
        )
        transport.release.set()
        first = await asyncio.wait_for(first_task, timeout=5)
    finally:
        transport.release.set()
        await first_engine.dispose()
        await second_engine.dispose()

    assert first.sent == 1
    assert second.sent == 0
    assert transport.calls == 1


async def test_retry_limit_becomes_failed(delivery_db: DeliveryDatabase) -> None:
    now = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC)
    seeded = await delivery_db.seed(
        fire_at=now,
        quiet_from=dt.time(1, 0),
        quiet_to=dt.time(2, 0),
    )
    transport = RecordingTransport(
        outcomes=[TransientDeliveryError("telegram.network") for _ in range(2)]
    )
    policy = RetryPolicy(base_delay_s=1, max_delay_s=1, max_attempts=2)

    await deliver_due(delivery_db.factory, transport, now_utc=now, retry_policy=policy)
    result = await deliver_due(
        delivery_db.factory,
        transport,
        now_utc=now + dt.timedelta(seconds=1),
        retry_policy=policy,
    )

    assert result.failed == 1
    stored = await _notification(delivery_db.factory, seeded.notification_id)
    assert stored.status is NotificationStatus.FAILED
    assert stored.attempt_count == 2
