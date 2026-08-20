"""Restart-safe delivery of persisted notifications.

The module deliberately knows nothing about aiogram.  A Telegram adapter implements
``NotificationTransport`` and translates provider failures to the typed errors below.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from seshat.db.enums import (
    AuditAction,
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
from seshat.domain.locks import lock_occurrence_action, lock_user_context
from seshat.domain.users import DomainError, is_quiet

_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_DELIVERY_LEASE = dt.timedelta(minutes=2)


@dataclass(frozen=True, slots=True)
class DeliveryCommand:
    notification_id: int
    occurrence_id: int
    user_id: int
    telegram_id: int
    title: str
    entry_kind: EntryKind
    notification_kind: NotificationKind
    planned_at_utc: dt.datetime
    fire_at_utc: dt.datetime
    silent: bool
    late: bool


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    message_id: int


class NotificationTransport(Protocol):
    async def send(self, command: DeliveryCommand) -> DeliveryReceipt: ...


class DeliveryTransportError(Exception):
    """Sanitized transport error safe to persist and log."""

    def __init__(self, code: str) -> None:
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("delivery error code must be a short normalized identifier")
        self.code = code
        super().__init__(code)


class TransientDeliveryError(DeliveryTransportError):
    def __init__(self, code: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(code)
        if retry_after_s is not None and retry_after_s < 1:
            raise ValueError("retry_after_s must be positive")
        self.retry_after_s = retry_after_s


class PermanentDeliveryError(DeliveryTransportError):
    pass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay_s: int = 30
    max_delay_s: int = 900
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.base_delay_s < 1 or self.max_delay_s < 1 or self.max_attempts < 1:
            raise ValueError("retry policy values must be positive")

    def delay_s(self, attempt_count: int, provider_delay_s: int | None = None) -> int:
        if provider_delay_s is not None:
            return min(provider_delay_s, self.max_delay_s)
        return min(self.base_delay_s * (2 ** max(0, attempt_count - 1)), self.max_delay_s)


@dataclass(frozen=True, slots=True)
class RepeatPolicy:
    interval_min: int = 15
    max_repeats: int = 3

    def __post_init__(self) -> None:
        if self.interval_min < 1 or self.max_repeats < 0:
            raise ValueError("invalid repeat policy")


@dataclass(frozen=True, slots=True)
class QuietWindow:
    active: bool
    end_at_utc: dt.datetime | None = None


@dataclass(frozen=True, slots=True)
class ScheduledDelivery:
    fire_at_utc: dt.datetime
    silent: bool


@dataclass(slots=True)
class DeliveryTickResult:
    sent: int = 0
    missed: int = 0
    retried: int = 0
    failed: int = 0
    cancelled: int = 0
    rescheduled: int = 0

    def record(self, outcome: str) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)


@dataclass(frozen=True, slots=True)
class NightDelivery:
    notification_id: int
    occurrence_id: int
    title: str
    sent_at_utc: dt.datetime


@dataclass(frozen=True, slots=True)
class RepeatCancellationResult:
    occurrence_id: int
    cancelled: int


@dataclass(frozen=True, slots=True)
class ActiveReactionResult:
    reacted: bool
    source: ReactionContextSource | None = None
    occurrence_id: int | None = None
    notification_id: int | None = None
    cancelled: int = 0


class ReactionContextSource(StrEnum):
    REPLY = "reply"
    EXPLICIT_NOTIFICATION = "explicit_notification"
    ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    command: DeliveryCommand


def quiet_window(
    moment_utc: dt.datetime,
    *,
    tz: str,
    quiet_from: dt.time,
    quiet_to: dt.time,
) -> QuietWindow:
    """Return the containing quiet interval and its DST-safe UTC end."""
    _require_aware(moment_utc)
    if quiet_from == quiet_to:
        raise ValueError("quiet interval bounds must differ")

    zone = ZoneInfo(tz)
    local = moment_utc.astimezone(zone)
    local_clock = local.timetz().replace(tzinfo=None)
    if not is_quiet(local_clock, quiet_from, quiet_to):
        return QuietWindow(active=False)

    end_date = local.date()
    if quiet_from > quiet_to and local_clock >= quiet_from:
        end_date += dt.timedelta(days=1)
    end_at = resolve_local_wall(end_date, quiet_to, zone, prefer_late=True)
    return QuietWindow(active=True, end_at_utc=end_at.astimezone(dt.UTC))


def completed_quiet_window(
    local_date: dt.date,
    *,
    tz: str,
    quiet_from: dt.time,
    quiet_to: dt.time,
) -> tuple[dt.datetime, dt.datetime]:
    """UTC-границы тихого интервала, завершившегося в ``local_date``."""
    if quiet_from == quiet_to:
        raise ValueError("quiet interval bounds must differ")
    zone = ZoneInfo(tz)
    start_date = local_date - dt.timedelta(days=1) if quiet_from > quiet_to else local_date
    start = resolve_local_wall(start_date, quiet_from, zone, prefer_late=False)
    end = resolve_local_wall(local_date, quiet_to, zone, prefer_late=True)
    return start.astimezone(dt.UTC), end.astimezone(dt.UTC)


def apply_quiet_policy(
    kind: NotificationKind,
    candidate_fire_at_utc: dt.datetime,
    occurrence_at_utc: dt.datetime,
    *,
    tz: str,
    quiet_from: dt.time,
    quiet_to: dt.time,
) -> ScheduledDelivery:
    """Apply the no-wake policy without touching persistence."""
    _require_aware(candidate_fire_at_utc)
    _require_aware(occurrence_at_utc)
    window = quiet_window(
        candidate_fire_at_utc,
        tz=tz,
        quiet_from=quiet_from,
        quiet_to=quiet_to,
    )
    if not window.active:
        return ScheduledDelivery(candidate_fire_at_utc, silent=False)
    assert window.end_at_utc is not None

    if kind is NotificationKind.MAIN:
        return ScheduledDelivery(candidate_fire_at_utc, silent=True)
    if kind is NotificationKind.PRE:
        if window.end_at_utc < occurrence_at_utc:
            return ScheduledDelivery(window.end_at_utc, silent=False)
        return ScheduledDelivery(candidate_fire_at_utc, silent=True)
    return ScheduledDelivery(window.end_at_utc, silent=False)


async def deliver_due(
    session_factory: async_sessionmaker[AsyncSession],
    transport: NotificationTransport,
    *,
    now_utc: dt.datetime | None = None,
    late_threshold_min: int = 30,
    batch_size: int = 20,
    retry_policy: RetryPolicy | None = None,
    repeat_policy: RepeatPolicy | None = None,
    active_context_ttl_min: int = 180,
) -> DeliveryTickResult:
    """Deliver at most ``batch_size`` rows, one locked transaction at a time."""
    now = now_utc or dt.datetime.now(dt.UTC)
    _require_aware(now)
    if late_threshold_min < 0 or batch_size < 1 or active_context_ttl_min < 1:
        raise ValueError("invalid delivery limits")
    retries = retry_policy or RetryPolicy()
    repeats = repeat_policy or RepeatPolicy()
    result = DeliveryTickResult()

    for _ in range(batch_size):
        outcome = await _deliver_one(
            session_factory,
            transport,
            now=now,
            late_threshold=dt.timedelta(minutes=late_threshold_min),
            retries=retries,
            repeats=repeats,
            active_context_ttl=dt.timedelta(minutes=active_context_ttl_min),
        )
        if outcome is None:
            break
        result.record(outcome)
    return result


async def cancel_pending_repeats(
    session: AsyncSession,
    occurrence_id: int,
    *,
    reacted_at_utc: dt.datetime,
) -> int:
    """Stop persistence after any user reaction.

    ``reacted_at_utc`` is validated now and reserved for the Stage 4 audit trail.
    """
    _require_aware(reacted_at_utc)
    result = await session.execute(
        update(Notification)
        .where(
            Notification.occurrence_id == occurrence_id,
            Notification.kind == NotificationKind.REPEAT,
            Notification.status == NotificationStatus.PENDING,
        )
        .values(status=NotificationStatus.CANCELLED, next_attempt_at_utc=None)
    )
    return int(result.rowcount or 0)


async def acknowledge_occurrence(
    session: AsyncSession,
    user_id: int,
    occurrence_id: int,
    *,
    reacted_at_utc: dt.datetime,
    clear_context: bool = True,
) -> RepeatCancellationResult:
    """Фиксирует реакцию и немедленно останавливает important-повторы."""
    _require_aware(reacted_at_utc)
    await lock_user_context(session, user_id)
    await lock_occurrence_action(session, occurrence_id)
    occurrence = await session.scalar(
        select(Occurrence)
        .where(Occurrence.id == occurrence_id, Occurrence.user_id == user_id)
        .with_for_update()
    )
    if occurrence is None:
        raise DomainError("occurrence not found")
    cancelled = await cancel_pending_repeats(
        session,
        occurrence_id,
        reacted_at_utc=reacted_at_utc,
    )
    context = await session.scalar(
        select(ActiveContext).where(ActiveContext.user_id == user_id).with_for_update()
    )
    if clear_context and context is not None and context.occurrence_id == occurrence_id:
        await session.delete(context)
    await _record_repeat_stop(
        session,
        user_id=user_id,
        occurrence_id=occurrence_id,
        reaction="acknowledge",
        cancelled=cancelled,
        reacted_at_utc=reacted_at_utc,
    )
    await session.flush()
    return RepeatCancellationResult(occurrence_id=occurrence_id, cancelled=cancelled)


async def react_to_active_context(
    session: AsyncSession,
    user_id: int,
    *,
    reacted_at_utc: dt.datetime,
) -> ActiveReactionResult:
    """Любая реакция в TTL активного напоминания немедленно гасит повторы."""
    _require_aware(reacted_at_utc)
    await lock_user_context(session, user_id)
    snapshot = (
        await session.execute(
            select(ActiveContext.occurrence_id, ActiveContext.notification_id).where(
                ActiveContext.user_id == user_id
            )
        )
    ).one_or_none()
    if snapshot is None:
        return ActiveReactionResult(reacted=False)
    snapshot_occurrence_id, snapshot_notification_id = snapshot

    # Общий lock order: UserContext -> Occurrence -> Notification/ActiveContext.
    await lock_occurrence_action(session, snapshot_occurrence_id)
    occurrence = await session.scalar(
        select(Occurrence)
        .where(Occurrence.id == snapshot_occurrence_id, Occurrence.user_id == user_id)
        .with_for_update()
    )
    context = await session.scalar(
        select(ActiveContext).where(ActiveContext.user_id == user_id).with_for_update()
    )
    if context is None or context.occurrence_id != snapshot_occurrence_id:
        return ActiveReactionResult(reacted=False)
    if context.expires_at_utc <= reacted_at_utc:
        await session.delete(context)
        await session.flush()
        return ActiveReactionResult(reacted=False)
    if occurrence is None:
        await session.delete(context)
        await session.flush()
        return ActiveReactionResult(reacted=False)

    cancelled = await cancel_pending_repeats(
        session,
        occurrence.id,
        reacted_at_utc=reacted_at_utc,
    )
    await session.delete(context)
    await _record_repeat_stop(
        session,
        user_id=user_id,
        occurrence_id=occurrence.id,
        reaction="active_context",
        cancelled=cancelled,
        reacted_at_utc=reacted_at_utc,
    )
    await session.flush()
    return ActiveReactionResult(
        reacted=True,
        source=ReactionContextSource.ACTIVE,
        occurrence_id=occurrence.id,
        notification_id=snapshot_notification_id,
        cancelled=cancelled,
    )


async def react_to_notification(
    session: AsyncSession,
    user_id: int,
    notification_id: int,
    *,
    reacted_at_utc: dt.datetime,
    source: ReactionContextSource = ReactionContextSource.EXPLICIT_NOTIFICATION,
) -> ActiveReactionResult:
    """Stop repeats for one explicitly referenced delivered notification."""
    _require_aware(reacted_at_utc)
    if notification_id < 1:
        return ActiveReactionResult(reacted=False)
    snapshot_occurrence_id = await session.scalar(
        select(Notification.occurrence_id).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    if snapshot_occurrence_id is None:
        return ActiveReactionResult(reacted=False)

    await lock_user_context(session, user_id)
    await lock_occurrence_action(session, snapshot_occurrence_id)
    occurrence = await session.scalar(
        select(Occurrence)
        .where(
            Occurrence.id == snapshot_occurrence_id,
            Occurrence.user_id == user_id,
        )
        .with_for_update()
    )
    notification = await session.scalar(
        select(Notification)
        .where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
            Notification.occurrence_id == snapshot_occurrence_id,
        )
        .with_for_update()
    )
    if occurrence is None or notification is None:
        return ActiveReactionResult(reacted=False)

    context = await session.scalar(
        select(ActiveContext).where(ActiveContext.user_id == user_id).with_for_update()
    )
    is_sent = notification.status is NotificationStatus.SENT
    is_in_flight = (
        notification.status is NotificationStatus.PENDING
        and context is not None
        and context.notification_id == notification.id
        and context.expires_at_utc > reacted_at_utc
    )
    if not (is_sent or is_in_flight):
        return ActiveReactionResult(reacted=False)

    cancelled = await cancel_pending_repeats(
        session,
        occurrence.id,
        reacted_at_utc=reacted_at_utc,
    )
    if context is not None and context.occurrence_id == occurrence.id:
        await session.delete(context)
    await _record_repeat_stop(
        session,
        user_id=user_id,
        occurrence_id=occurrence.id,
        reaction=source.value,
        cancelled=cancelled,
        reacted_at_utc=reacted_at_utc,
    )
    await session.flush()
    return ActiveReactionResult(
        reacted=True,
        source=source,
        occurrence_id=occurrence.id,
        notification_id=notification.id,
        cancelled=cancelled,
    )


async def react_to_notification_reply(
    session: AsyncSession,
    user_id: int,
    telegram_message_id: int,
    *,
    reacted_at_utc: dt.datetime,
) -> ActiveReactionResult:
    """Resolve an explicit Telegram reply to its persisted notification."""
    if telegram_message_id < 1:
        return ActiveReactionResult(reacted=False)
    notification_id = await session.scalar(
        select(Notification.id)
        .where(
            Notification.user_id == user_id,
            Notification.telegram_message_id == telegram_message_id,
        )
        .order_by(Notification.id.desc())
        .limit(1)
    )
    if notification_id is None:
        return ActiveReactionResult(reacted=False)
    return await react_to_notification(
        session,
        user_id,
        notification_id,
        reacted_at_utc=reacted_at_utc,
        source=ReactionContextSource.REPLY,
    )


async def react_to_reaction_context(
    session: AsyncSession,
    user_id: int,
    *,
    notification_id: int | None,
    reacted_at_utc: dt.datetime,
) -> ActiveReactionResult:
    """Resolve an explicit notification id or consume the current active context."""
    if notification_id is not None:
        return await react_to_notification(
            session,
            user_id,
            notification_id,
            reacted_at_utc=reacted_at_utc,
        )
    return await react_to_active_context(
        session,
        user_id,
        reacted_at_utc=reacted_at_utc,
    )


async def react_to_message_context(
    session: AsyncSession,
    user_id: int,
    *,
    reply_telegram_message_id: int | None,
    reacted_at_utc: dt.datetime,
) -> ActiveReactionResult:
    """Prefer an explicit reply; otherwise consume the live three-hour context."""
    if reply_telegram_message_id is not None:
        return await react_to_notification_reply(
            session,
            user_id,
            reply_telegram_message_id,
            reacted_at_utc=reacted_at_utc,
        )
    return await react_to_active_context(
        session,
        user_id,
        reacted_at_utc=reacted_at_utc,
    )


async def _record_repeat_stop(
    session: AsyncSession,
    *,
    user_id: int,
    occurrence_id: int,
    reaction: str,
    cancelled: int,
    reacted_at_utc: dt.datetime,
) -> None:
    existing = await session.scalar(
        select(AuditLog.id).where(
            AuditLog.user_id == user_id,
            AuditLog.entity == "occurrence_reaction",
            AuditLog.entity_id == occurrence_id,
        )
    )
    if existing is not None:
        return
    session.add(
        AuditLog(
            user_id=user_id,
            entity="occurrence_reaction",
            entity_id=occurrence_id,
            action=AuditAction.UPDATE,
            payload={
                "reaction": reaction,
                "repeats_cancelled": cancelled,
                "at": reacted_at_utc.astimezone(dt.UTC).isoformat(),
            },
        )
    )


async def list_night_deliveries(
    session: AsyncSession,
    user_id: int,
    *,
    window_start_utc: dt.datetime,
    window_end_utc: dt.datetime,
) -> list[NightDelivery]:
    """Return silent deliveries for the morning digest, oldest first."""
    _require_aware(window_start_utc)
    _require_aware(window_end_utc)
    if window_end_utc <= window_start_utc:
        raise ValueError("night digest window must be positive")
    rows = (
        await session.execute(
            select(Notification.id, Occurrence.id, Entry.title, Notification.sent_at_utc)
            .join(Occurrence, Occurrence.id == Notification.occurrence_id)
            .join(Entry, Entry.id == Occurrence.entry_id)
            .where(
                Notification.user_id == user_id,
                Notification.status == NotificationStatus.SENT,
                Notification.silent.is_(True),
                Notification.sent_at_utc >= window_start_utc,
                Notification.sent_at_utc < window_end_utc,
            )
            .order_by(Notification.sent_at_utc, Notification.id)
        )
    ).all()
    return [NightDelivery(n_id, o_id, title, sent_at) for n_id, o_id, title, sent_at in rows]


async def _deliver_one(
    session_factory: async_sessionmaker[AsyncSession],
    transport: NotificationTransport,
    *,
    now: dt.datetime,
    late_threshold: dt.timedelta,
    retries: RetryPolicy,
    repeats: RepeatPolicy,
    active_context_ttl: dt.timedelta,
) -> str | None:
    claimed = await _claim_one(
        session_factory,
        now=now,
        late_threshold=late_threshold,
        active_context_ttl=active_context_ttl,
    )
    if claimed is None or isinstance(claimed, str):
        return claimed

    try:
        receipt = await transport.send(claimed.command)
    except PermanentDeliveryError as exc:
        return await _finalize_failed_delivery(
            session_factory,
            claimed,
            exc,
            now=now,
            retries=retries,
            permanent=True,
        )
    except TransientDeliveryError as exc:
        return await _finalize_failed_delivery(
            session_factory,
            claimed,
            exc,
            now=now,
            retries=retries,
            permanent=False,
        )

    return await _finalize_sent_delivery(
        session_factory,
        claimed,
        receipt,
        now=now,
        repeats=repeats,
    )


async def _claim_one(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: dt.datetime,
    late_threshold: dt.timedelta,
    active_context_ttl: dt.timedelta,
) -> ClaimedDelivery | str | None:
    """Фиксирует контекст до Telegram send и ставит короткую delivery-lease."""
    async with session_factory() as session, session.begin():
        candidate = (
            await session.execute(
                select(Notification.id, Notification.occurrence_id, Notification.user_id)
                .where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.fire_at_utc <= now,
                    or_(
                        Notification.next_attempt_at_utc.is_(None),
                        Notification.next_attempt_at_utc <= now,
                    ),
                )
                .order_by(Notification.fire_at_utc, Notification.id)
                .limit(1)
            )
        ).one_or_none()
        if candidate is None:
            return None
        notification_id, occurrence_id, user_id = candidate
        await lock_user_context(session, user_id)
        await lock_occurrence_action(session, occurrence_id)
        occurrence = await session.scalar(
            select(Occurrence).where(Occurrence.id == occurrence_id).with_for_update()
        )
        if occurrence is None:
            return None
        notification = await session.scalar(
            select(Notification)
            .where(
                Notification.id == notification_id,
                Notification.occurrence_id == occurrence_id,
                Notification.status == NotificationStatus.PENDING,
                Notification.fire_at_utc <= now,
                or_(
                    Notification.next_attempt_at_utc.is_(None),
                    Notification.next_attempt_at_utc <= now,
                ),
            )
            .with_for_update()
        )
        if notification is None:
            return None
        entry = await session.get(Entry, occurrence.entry_id)
        user = await session.get(User, notification.user_id)
        settings = await session.get(UserSettings, notification.user_id)
        if entry is None or user is None or settings is None:
            notification.status = NotificationStatus.FAILED
            notification.last_error_code = "delivery_contract_missing"
            notification.next_attempt_at_utc = None
            return "failed"

        if occurrence.status is not OccurrenceStatus.PENDING:
            notification.status = NotificationStatus.CANCELLED
            notification.next_attempt_at_utc = None
            return "cancelled"
        if notification.kind is NotificationKind.REPEAT and await _repeat_stopped(
            session, occurrence.id
        ):
            notification.status = NotificationStatus.CANCELLED
            notification.next_attempt_at_utc = None
            return "cancelled"

        planned_policy = apply_quiet_policy(
            notification.kind,
            notification.fire_at_utc,
            occurrence.planned_at_utc,
            tz=settings.tz,
            quiet_from=settings.quiet_from,
            quiet_to=settings.quiet_to,
        )
        actual_quiet = quiet_window(
            now,
            tz=settings.tz,
            quiet_from=settings.quiet_from,
            quiet_to=settings.quiet_to,
        )
        reschedule_at = (
            planned_policy.fire_at_utc
            if planned_policy.fire_at_utc > notification.fire_at_utc
            else None
        )
        # PRE переносится только потому, что его плановый fire_at был ночью.
        # Задержавшийся на пару секунд вечерний PRE не должен внезапно уехать
        # на утро. Для REPEAT правило строже: фактической отправки ночью нет.
        if (
            reschedule_at is None
            and notification.kind is NotificationKind.REPEAT
            and actual_quiet.active
        ):
            reschedule_at = actual_quiet.end_at_utc
        if reschedule_at is not None:
            duplicate = await session.scalar(
                select(Notification.id).where(
                    Notification.id != notification.id,
                    Notification.occurrence_id == notification.occurrence_id,
                    Notification.kind == notification.kind,
                    Notification.fire_at_utc == reschedule_at,
                )
            )
            if duplicate is not None:
                notification.status = NotificationStatus.CANCELLED
                return "cancelled"
            notification.fire_at_utc = reschedule_at
            notification.silent = False
            notification.next_attempt_at_utc = None
            if reschedule_at > now:
                return "rescheduled"

        lateness = now - notification.fire_at_utc
        if lateness > late_threshold:
            notification.status = NotificationStatus.MISSED
            notification.next_attempt_at_utc = None
            if notification.kind is NotificationKind.MAIN:
                occurrence.status = OccurrenceStatus.MISSED
            return "missed"

        silent = notification.silent or planned_policy.silent or actual_quiet.active
        command = DeliveryCommand(
            notification_id=notification.id,
            occurrence_id=occurrence.id,
            user_id=notification.user_id,
            telegram_id=user.telegram_id,
            title=entry.title,
            entry_kind=entry.kind,
            notification_kind=notification.kind,
            planned_at_utc=occurrence.planned_at_utc,
            fire_at_utc=notification.fire_at_utc,
            silent=silent,
            late=lateness > dt.timedelta(0),
        )
        notification.silent = silent
        notification.next_attempt_at_utc = now + _DELIVERY_LEASE
        await _set_active_context(
            session,
            notification,
            expires_at=now + active_context_ttl,
        )
        return ClaimedDelivery(command=command)


async def _finalize_sent_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedDelivery,
    receipt: DeliveryReceipt,
    *,
    now: dt.datetime,
    repeats: RepeatPolicy,
) -> str:
    async with session_factory() as session, session.begin():
        await lock_occurrence_action(session, claimed.command.occurrence_id)
        occurrence = await session.scalar(
            select(Occurrence)
            .where(Occurrence.id == claimed.command.occurrence_id)
            .with_for_update()
        )
        if occurrence is None:
            return "failed"
        notification = await session.scalar(
            select(Notification)
            .where(
                Notification.id == claimed.command.notification_id,
                Notification.occurrence_id == occurrence.id,
            )
            .with_for_update()
        )
        if notification is None:
            return "failed"
        entry = await session.get(Entry, occurrence.entry_id)
        settings = await session.get(UserSettings, notification.user_id)
        if entry is None or settings is None:
            return "failed"
        notification.status = NotificationStatus.SENT
        notification.silent = claimed.command.silent
        notification.sent_at_utc = now
        notification.telegram_message_id = receipt.message_id
        notification.next_attempt_at_utc = None
        notification.last_error_code = None
        if occurrence.status is OccurrenceStatus.PENDING:
            await _schedule_next_repeat(
                session,
                notification=notification,
                occurrence=occurrence,
                entry=entry,
                settings=settings,
                sent_at=now,
                policy=repeats,
            )
        return "sent"


async def _finalize_failed_delivery(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: ClaimedDelivery,
    error: DeliveryTransportError,
    *,
    now: dt.datetime,
    retries: RetryPolicy,
    permanent: bool,
) -> str:
    async with session_factory() as session, session.begin():
        await lock_user_context(session, claimed.command.user_id)
        await lock_occurrence_action(session, claimed.command.occurrence_id)
        occurrence = await session.scalar(
            select(Occurrence)
            .where(Occurrence.id == claimed.command.occurrence_id)
            .with_for_update()
        )
        if occurrence is None:
            return "failed"
        notification = await session.scalar(
            select(Notification)
            .where(
                Notification.id == claimed.command.notification_id,
                Notification.occurrence_id == occurrence.id,
            )
            .with_for_update()
        )
        if notification is None:
            return "failed"
        await _clear_active_context_for_notification(session, notification)
        if notification.status is not NotificationStatus.PENDING:
            return "cancelled"

        _record_failed_attempt(notification, error.code)
        if permanent or notification.attempt_count >= retries.max_attempts:
            notification.status = NotificationStatus.FAILED
            return "failed"
        provider_delay = error.retry_after_s if isinstance(error, TransientDeliveryError) else None
        delay = retries.delay_s(notification.attempt_count, provider_delay)
        notification.next_attempt_at_utc = now + dt.timedelta(seconds=delay)
        return "retried"


def _record_failed_attempt(notification: Notification, code: str) -> None:
    notification.attempt_count += 1
    notification.last_error_code = code
    notification.next_attempt_at_utc = None


async def _set_active_context(
    session: AsyncSession,
    notification: Notification,
    *,
    expires_at: dt.datetime,
) -> None:
    statement = pg_insert(ActiveContext).values(
        user_id=notification.user_id,
        occurrence_id=notification.occurrence_id,
        notification_id=notification.id,
        expires_at_utc=expires_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[ActiveContext.user_id],
        set_={
            "occurrence_id": statement.excluded.occurrence_id,
            "notification_id": statement.excluded.notification_id,
            "expires_at_utc": statement.excluded.expires_at_utc,
        },
    )
    await session.execute(statement)


async def _clear_active_context_for_notification(
    session: AsyncSession,
    notification: Notification,
) -> None:
    context = await session.scalar(
        select(ActiveContext).where(ActiveContext.user_id == notification.user_id).with_for_update()
    )
    if context is not None and context.notification_id == notification.id:
        await session.delete(context)


async def _repeat_stopped(session: AsyncSession, occurrence_id: int) -> bool:
    marker = await session.scalar(
        select(AuditLog.id).where(
            AuditLog.entity == "occurrence_reaction",
            AuditLog.entity_id == occurrence_id,
        )
    )
    return marker is not None


async def _schedule_next_repeat(
    session: AsyncSession,
    *,
    notification: Notification,
    occurrence: Occurrence,
    entry: Entry,
    settings: UserSettings,
    sent_at: dt.datetime,
    policy: RepeatPolicy,
) -> None:
    if (
        entry.persistence is not Persistence.IMPORTANT
        or notification.kind not in {NotificationKind.MAIN, NotificationKind.REPEAT}
        or policy.max_repeats == 0
    ):
        return
    if await _repeat_stopped(session, occurrence.id):
        return
    existing = await session.scalar(
        select(func.count(Notification.id)).where(
            Notification.occurrence_id == occurrence.id,
            Notification.kind == NotificationKind.REPEAT,
        )
    )
    if int(existing or 0) >= policy.max_repeats:
        return
    candidate = sent_at + dt.timedelta(minutes=policy.interval_min)
    scheduled = apply_quiet_policy(
        NotificationKind.REPEAT,
        candidate,
        occurrence.planned_at_utc,
        tz=settings.tz,
        quiet_from=settings.quiet_from,
        quiet_to=settings.quiet_to,
    )
    duplicate = await session.scalar(
        select(Notification.id).where(
            Notification.occurrence_id == occurrence.id,
            Notification.kind == NotificationKind.REPEAT,
            Notification.fire_at_utc == scheduled.fire_at_utc,
        )
    )
    if duplicate is None:
        session.add(
            Notification(
                occurrence_id=occurrence.id,
                user_id=occurrence.user_id,
                fire_at_utc=scheduled.fire_at_utc,
                kind=NotificationKind.REPEAT,
                silent=scheduled.silent,
            )
        )


def resolve_local_wall(
    local_date: dt.date,
    local_time: dt.time,
    zone: ZoneInfo,
    *,
    prefer_late: bool,
) -> dt.datetime:
    """Resolve a wall time; gaps roll forward, folds choose the requested side."""
    for minute_offset in range(181):
        shifted_time = dt.datetime.combine(local_date, local_time, tzinfo=zone) + dt.timedelta(
            minutes=minute_offset
        )
        candidates: list[dt.datetime] = []
        for fold in (0, 1):
            candidate = shifted_time.replace(fold=fold)
            roundtrip = candidate.astimezone(dt.UTC).astimezone(zone)
            if (
                roundtrip.date() == candidate.date()
                and roundtrip.hour == candidate.hour
                and roundtrip.minute == candidate.minute
            ):
                candidates.append(candidate)
        if candidates:
            unique = {candidate.astimezone(dt.UTC): candidate for candidate in candidates}
            ordered = sorted(unique.values(), key=lambda item: item.astimezone(dt.UTC))
            return ordered[-1] if prefer_late else ordered[0]
    raise ValueError("could not resolve local wall time within three hours")


def _require_aware(value: dt.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
