"""Интеграция подтверждения записи, runtime и Telegram-презентации."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import func, select

from seshat.config import Settings
from seshat.db.base import make_session_factory
from seshat.db.enums import EntryKind, NotificationKind
from seshat.db.models import Notification, Occurrence, User, UserSettings
from seshat.domain.delivery import DeliveryCommand, DeliveryReceipt
from seshat.domain.digests import MorningDigestCommand
from seshat.domain.entries import ManualEntryInput, prepare_manual_entry
from seshat.domain.planning import create_planned_entry
from seshat.domain.scheduling import ReminderDefaults
from seshat.domain.users import DomainError
from seshat.scheduler import SchedulerRuntime
from seshat.telegram.delivery import notification_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)


class RecordingTransport:
    def __init__(self) -> None:
        self.commands: list[DeliveryCommand] = []

    async def send(self, command: DeliveryCommand) -> DeliveryReceipt:
        self.commands.append(command)
        return DeliveryReceipt(message_id=len(self.commands))

    async def send_digest(self, command: MorningDigestCommand) -> DeliveryReceipt:
        return DeliveryReceipt(message_id=10_000 + command.user_id)


def _settings(database_url: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="123456:TEST",
        telegram_owner_id=123456789,
        database_url=database_url,
        default_event_reminders_min=(15,),
    )


@pytest.mark.asyncio
async def test_confirmed_entry_is_scheduled_in_same_transaction(session) -> None:
    now = dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC)
    user = User(telegram_id=123456789)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, tz="Europe/Moscow"))
    draft = prepare_manual_entry(
        ManualEntryInput(
            kind=EntryKind.EVENT,
            title="Созвон",
            start=dt.datetime(2026, 8, 3, 14, 0),
        ),
        tz="Europe/Moscow",
        now_utc=now,
    )

    first = await create_planned_entry(
        session,
        user.id,
        draft,
        confirmation_id=uuid.uuid4(),
        now_utc=now,
        horizon_days=14,
        late_lookback_minutes=30,
        defaults=ReminderDefaults(),
    )

    occurrence = await session.scalar(
        select(Occurrence).where(Occurrence.entry_id == first.entry.id)
    )
    assert occurrence is not None
    notifications = (
        (
            await session.execute(
                select(Notification)
                .where(Notification.occurrence_id == occurrence.id)
                .order_by(Notification.fire_at_utc)
            )
        )
        .scalars()
        .all()
    )
    assert [(item.kind, item.fire_at_utc) for item in notifications] == [
        (NotificationKind.PRE, dt.datetime(2026, 8, 3, 10, 45, tzinfo=dt.UTC)),
        (NotificationKind.MAIN, dt.datetime(2026, 8, 3, 11, 0, tzinfo=dt.UTC)),
    ]


@pytest.mark.asyncio
async def test_stale_timezone_card_cannot_create_old_routine_schedule(session) -> None:
    now = dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC)
    user = User(telegram_id=494201712)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, tz="Europe/Amsterdam"))
    await session.flush()
    stale = prepare_manual_entry(
        ManualEntryInput(
            kind=EntryKind.ROUTINE,
            title="Зарядка",
            start=dt.datetime(2026, 8, 4, 8, 0),
            recurrence={"freq": "daily"},
        ),
        tz="Europe/Moscow",
        now_utc=now,
    )

    with pytest.raises(DomainError, match="устарела"):
        await create_planned_entry(
            session,
            user.id,
            stale,
            confirmation_id=uuid.uuid4(),
            now_utc=now,
            horizon_days=14,
            late_lookback_minutes=30,
            defaults=ReminderDefaults(),
        )


@pytest.mark.asyncio
async def test_runtime_reconciliation_is_idempotent(session_factory, engine) -> None:
    async with session_factory() as session, session.begin():
        user = User(telegram_id=494201711)
        session.add(user)
        await session.flush()
        session.add(UserSettings(user_id=user.id, tz="Europe/Moscow"))
        draft = prepare_manual_entry(
            ManualEntryInput(
                kind=EntryKind.EVENT,
                title="Встреча",
                start=dt.datetime(2026, 8, 4, 15, 0),
            ),
            tz="Europe/Moscow",
            now_utc=dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC),
        )
        await create_planned_entry(
            session,
            user.id,
            draft,
            confirmation_id=uuid.uuid4(),
            now_utc=dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC),
            horizon_days=14,
            late_lookback_minutes=30,
            defaults=ReminderDefaults(),
        )
        user_id = user.id

    runtime = SchedulerRuntime(
        _settings(str(engine.url)),
        session_factory,
        RecordingTransport(),
        clock=lambda: dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC),
    )
    await runtime.reconcile()
    await runtime.reconcile()

    async with session_factory() as session:
        assert (
            await session.scalar(
                select(func.count(Occurrence.id)).where(Occurrence.user_id == user_id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count(Notification.id)).where(Notification.user_id == user_id)
            )
            == 2
        )


@pytest.mark.asyncio
async def test_runtime_loop_stops_without_waiting_for_intervals(engine) -> None:
    stop = asyncio.Event()
    runtime = SchedulerRuntime(
        _settings(str(engine.url)),
        make_session_factory(engine),
        RecordingTransport(),
        clock=lambda: dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC),
        reconcile_interval_s=3600,
    )

    task = asyncio.create_task(runtime.run(stop_event=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=2)


def test_notification_text_is_html_safe_and_marks_late() -> None:
    moment = dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.UTC)
    text = notification_text(
        DeliveryCommand(
            notification_id=1,
            occurrence_id=2,
            user_id=3,
            telegram_id=4,
            title="<b>не доверять</b>",
            entry_kind=EntryKind.EVENT,
            notification_kind=NotificationKind.MAIN,
            planned_at_utc=moment,
            fire_at_utc=moment,
            silent=True,
            late=True,
        )
    )
    assert text.startswith("⚠️ Доставлено с опозданием")
    assert "&lt;b&gt;не доверять&lt;/b&gt;" in text
    assert text.count("<b>") == 1

    repeat = notification_text(
        DeliveryCommand(
            notification_id=2,
            occurrence_id=42,
            user_id=3,
            telegram_id=4,
            title="Зарядка",
            entry_kind=EntryKind.ROUTINE,
            notification_kind=NotificationKind.REPEAT,
            planned_at_utc=moment,
            fire_at_utc=moment,
            silent=False,
            late=False,
        )
    )
    assert "/skip 42" in repeat
    assert "/ack 42" in repeat
