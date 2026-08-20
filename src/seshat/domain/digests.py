"""Durable morning digest for nightly silent deliveries."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from seshat.db.enums import AuditAction, NotificationStatus
from seshat.db.models import AuditLog, Entry, Notification, Occurrence, User, UserSettings
from seshat.domain.day import MyDay, MyDayNightItem
from seshat.domain.delivery import (
    DeliveryReceipt,
    PermanentDeliveryError,
    TransientDeliveryError,
    quiet_window,
    resolve_local_wall,
)

_NIGHT_BATCH_SIZE = 3
_TRANSIENT_RETRY_DELAY = dt.timedelta(minutes=5)
_PERMANENT_RETRY_DELAY = dt.timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class MorningDigestCommand:
    user_id: int
    telegram_id: int
    day: MyDay


class MorningDigestTransport(Protocol):
    async def send_digest(self, command: MorningDigestCommand) -> DeliveryReceipt: ...


@dataclass(slots=True)
class DigestTickResult:
    sent: int = 0
    empty: int = 0
    retried: int = 0
    failed: int = 0

    def record(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)


def digest_due_at_utc(settings: UserSettings, local_date: dt.date) -> dt.datetime:
    """Вычисляет UTC-момент из локального digest_time при каждом tick."""
    zone = ZoneInfo(settings.tz)
    candidate = resolve_local_wall(
        local_date,
        settings.digest_time,
        zone,
        prefer_late=True,
    ).astimezone(dt.UTC)
    window = quiet_window(
        candidate,
        tz=settings.tz,
        quiet_from=settings.quiet_from,
        quiet_to=settings.quiet_to,
    )
    if window.active:
        assert window.end_at_utc is not None
        return window.end_at_utc
    return candidate


def _digest_due_for_now(settings: UserSettings, now: dt.datetime) -> dt.datetime:
    """Возвращает due текущего местного утра, включая вечерний time в quiet."""
    zone = ZoneInfo(settings.tz)
    local_date = now.astimezone(zone).date()
    today_due = digest_due_at_utc(settings, local_date)
    previous_due = digest_due_at_utc(settings, local_date - dt.timedelta(days=1))
    if previous_due.astimezone(zone).date() == local_date:
        return previous_due
    return today_due


async def deliver_due_morning_digests(
    session_factory: async_sessionmaker[AsyncSession],
    transport: MorningDigestTransport,
    *,
    now_utc: dt.datetime | None = None,
    batch_size: int = 20,
) -> DigestTickResult:
    """Отправляет не более одного утреннего дайджеста на пользователя и день."""
    now = now_utc or dt.datetime.now(dt.UTC)
    _require_aware(now)
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    async with session_factory() as session:
        user_ids = (
            await session.scalars(select(User.id).join(UserSettings).order_by(User.id))
        ).all()

    result = DigestTickResult()
    for user_id in user_ids:
        outcome = await _deliver_user_digest(
            session_factory,
            transport,
            user_id=user_id,
            now=now,
        )
        if outcome is not None:
            result.record(outcome)
            if sum((result.sent, result.empty, result.retried, result.failed)) >= batch_size:
                break
    return result


async def _deliver_user_digest(
    session_factory: async_sessionmaker[AsyncSession],
    transport: MorningDigestTransport,
    *,
    user_id: int,
    now: dt.datetime,
) -> str | None:
    async with session_factory() as session, session.begin():
        selected = (
            await session.execute(
                select(User, UserSettings)
                .join(UserSettings, UserSettings.user_id == User.id)
                .where(User.id == user_id)
                .with_for_update(of=UserSettings)
            )
        ).one_or_none()
        if selected is None:
            return None
        user, settings = selected
        zone = ZoneInfo(settings.tz)
        local_date = now.astimezone(zone).date()
        if now < _digest_due_for_now(settings, now):
            return None
        if quiet_window(
            now,
            tz=settings.tz,
            quiet_from=settings.quiet_from,
            quiet_to=settings.quiet_to,
        ).active:
            return None

        ledger = await session.scalar(
            select(AuditLog.id).where(
                AuditLog.user_id == user_id,
                AuditLog.entity == "morning_digest",
                AuditLog.entity_id == local_date.toordinal(),
            )
        )
        claimed = (
            await session.execute(
                select(Notification, Occurrence, Entry)
                .join(Occurrence, Occurrence.id == Notification.occurrence_id)
                .join(Entry, Entry.id == Occurrence.entry_id)
                .where(
                    Notification.user_id == user_id,
                    Notification.status == NotificationStatus.SENT,
                    Notification.silent.is_(True),
                    Notification.digest_included_at_utc.is_(None),
                    Notification.sent_at_utc <= now,
                    or_(
                        Notification.digest_next_attempt_at_utc.is_(None),
                        Notification.digest_next_attempt_at_utc <= now,
                    ),
                )
                .order_by(Notification.sent_at_utc, Notification.id)
                .limit(_NIGHT_BATCH_SIZE)
                .with_for_update(of=Notification)
            )
        ).all()

        if not claimed:
            return None

        night = tuple(
            MyDayNightItem(
                notification_id=notification.id,
                occurrence_id=occurrence.id,
                title=entry.title,
                sent_at_utc=notification.sent_at_utc,
                sent_at_local=notification.sent_at_utc.astimezone(zone),
            )
            for notification, occurrence, entry in claimed
            if notification.sent_at_utc is not None
        )
        digest_day = MyDay(
            local_date=local_date,
            tz=settings.tz,
            missed=(),
            items=(),
            night=night,
        )

        try:
            receipt = await transport.send_digest(
                MorningDigestCommand(
                    user_id=user_id,
                    telegram_id=user.telegram_id,
                    day=digest_day,
                )
            )
        except TransientDeliveryError:
            _defer_claimed(claimed, now + _TRANSIENT_RETRY_DELAY)
            return "retried"
        except PermanentDeliveryError:
            _defer_claimed(claimed, now + _PERMANENT_RETRY_DELAY)
            if ledger is None:
                _add_ledger(session, user_id, local_date, now, status="failed")
            return "failed"

        for notification, _, _ in claimed:
            notification.digest_included_at_utc = now
            notification.digest_next_attempt_at_utc = None
        if ledger is None:
            _add_ledger(
                session,
                user_id,
                local_date,
                now,
                status="sent",
                message_id=receipt.message_id,
                item_count=len(night),
            )
        else:
            session.add(
                AuditLog(
                    user_id=user_id,
                    entity="morning_digest_delta",
                    entity_id=night[0].notification_id if night else None,
                    action=AuditAction.CREATE,
                    payload={
                        "local_date": local_date.isoformat(),
                        "sent_at": now.isoformat(),
                        "message_id": receipt.message_id,
                        "item_count": len(night),
                    },
                )
            )
        await session.flush()
        return "sent"


def _defer_claimed(
    claimed: list[tuple[Notification, Occurrence, Entry]],
    next_attempt_at_utc: dt.datetime,
) -> None:
    for notification, _, _ in claimed:
        notification.digest_attempt_count += 1
        notification.digest_next_attempt_at_utc = next_attempt_at_utc


def _add_ledger(
    session: AsyncSession,
    user_id: int,
    local_date: dt.date,
    now: dt.datetime,
    *,
    status: str,
    message_id: int | None = None,
    item_count: int = 0,
) -> None:
    session.add(
        AuditLog(
            user_id=user_id,
            entity="morning_digest",
            entity_id=local_date.toordinal(),
            action=AuditAction.CREATE,
            payload={
                "local_date": local_date.isoformat(),
                "status": status,
                "at": now.isoformat(),
                "message_id": message_id,
                "item_count": item_count,
            },
        )
    )


def _require_aware(value: dt.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


__all__ = [
    "DigestTickResult",
    "MorningDigestCommand",
    "MorningDigestTransport",
    "deliver_due_morning_digests",
    "digest_due_at_utc",
]
