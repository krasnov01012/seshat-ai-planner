"""Deterministic notification actions used by Telegram buttons and API."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.enums import (
    AuditAction,
    EntryKind,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
)
from seshat.db.models import ActiveContext, AuditLog, Entry, Notification, Occurrence, UserSettings
from seshat.domain.delivery import acknowledge_occurrence
from seshat.domain.locks import lock_occurrence_action, lock_user_context
from seshat.domain.scheduling import ReminderDefaults, resolve_wall_time, schedule_notifications
from seshat.domain.users import DomainError


class ReactionAction(StrEnum):
    COMPLETE = "complete"
    SNOOZE = "snooze"
    MOVE = "move"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class ReactionResult:
    source_notification_id: int
    occurrence_id: int
    action: ReactionAction
    changed: bool
    status: OccurrenceStatus
    cancelled_notifications: int = 0
    scheduled_notification_id: int | None = None
    successor_occurrence_id: int | None = None
    target_at_utc: dt.datetime | None = None
    moved_count: int = 0


@dataclass(frozen=True, slots=True)
class MovePreview:
    source_notification_id: int
    occurrence_id: int
    target_at_utc: dt.datetime
    target_local: dt.datetime
    tz: str


async def apply_notification_action(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    action: ReactionAction,
    *,
    reacted_at_utc: dt.datetime,
    target_at_utc: dt.datetime | None = None,
    defaults: ReminderDefaults | None = None,
) -> ReactionResult:
    """Unified API-first entry point for every deterministic button action."""
    if action is ReactionAction.COMPLETE:
        return await complete_from_notification(
            session, user_id, source_notification_id, reacted_at_utc=reacted_at_utc
        )
    if action is ReactionAction.SNOOZE:
        return await snooze_from_notification(
            session,
            user_id,
            source_notification_id,
            reacted_at_utc=reacted_at_utc,
            minutes=60,
        )
    if action is ReactionAction.SKIP:
        return await skip_from_notification(
            session, user_id, source_notification_id, reacted_at_utc=reacted_at_utc
        )
    if target_at_utc is None or defaults is None:
        raise DomainError("move requires target_at_utc and reminder defaults")
    return await move_from_notification(
        session,
        user_id,
        source_notification_id,
        target_at_utc,
        reacted_at_utc=reacted_at_utc,
        defaults=defaults,
    )


async def complete_from_notification(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    *,
    reacted_at_utc: dt.datetime,
) -> ReactionResult:
    source, occurrence, _entry, replay = await _lock_source(
        session,
        user_id,
        source_notification_id,
        ReactionAction.COMPLETE,
        reacted_at_utc=reacted_at_utc,
    )
    if replay is not None:
        return replay
    if occurrence.status is OccurrenceStatus.DONE:
        return await _record_action(
            session, source, occurrence, ReactionAction.COMPLETE, changed=False
        )
    _require_actionable(occurrence)
    acknowledged = await acknowledge_occurrence(
        session, user_id, occurrence.id, reacted_at_utc=reacted_at_utc
    )
    cancelled = acknowledged.cancelled + await _cancel_pending(session, occurrence.id)
    occurrence.status = OccurrenceStatus.DONE
    occurrence.completed_at_utc = reacted_at_utc
    return await _record_action(
        session,
        source,
        occurrence,
        ReactionAction.COMPLETE,
        changed=True,
        cancelled=cancelled,
    )


async def skip_from_notification(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    *,
    reacted_at_utc: dt.datetime,
) -> ReactionResult:
    source, occurrence, _entry, replay = await _lock_source(
        session,
        user_id,
        source_notification_id,
        ReactionAction.SKIP,
        reacted_at_utc=reacted_at_utc,
    )
    if replay is not None:
        return replay
    if occurrence.status is OccurrenceStatus.SKIPPED:
        return await _record_action(session, source, occurrence, ReactionAction.SKIP, changed=False)
    _require_actionable(occurrence)
    acknowledged = await acknowledge_occurrence(
        session, user_id, occurrence.id, reacted_at_utc=reacted_at_utc
    )
    cancelled = acknowledged.cancelled + await _cancel_pending(session, occurrence.id)
    occurrence.status = OccurrenceStatus.SKIPPED
    return await _record_action(
        session,
        source,
        occurrence,
        ReactionAction.SKIP,
        changed=True,
        cancelled=cancelled,
    )


async def snooze_from_notification(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    *,
    reacted_at_utc: dt.datetime,
    minutes: int | None = None,
) -> ReactionResult:
    source, occurrence, _entry, replay = await _lock_source(
        session,
        user_id,
        source_notification_id,
        ReactionAction.SNOOZE,
        reacted_at_utc=reacted_at_utc,
    )
    if replay is not None:
        return replay
    _require_actionable(occurrence)
    settings = await session.get(UserSettings, user_id)
    if settings is None:
        raise DomainError("user settings not found")
    snooze_min = minutes if minutes is not None else settings.default_snooze_min
    if snooze_min < 1:
        raise DomainError("snooze must be positive")
    target = reacted_at_utc + dt.timedelta(minutes=snooze_min)
    acknowledged = await acknowledge_occurrence(
        session, user_id, occurrence.id, reacted_at_utc=reacted_at_utc
    )
    cancelled = acknowledged.cancelled + await _cancel_pending(session, occurrence.id)
    existing = await session.scalar(
        select(Notification).where(
            Notification.occurrence_id == occurrence.id,
            Notification.fire_at_utc == target,
            Notification.kind == NotificationKind.MAIN,
        )
    )
    if existing is None:
        existing = Notification(
            occurrence_id=occurrence.id,
            user_id=user_id,
            fire_at_utc=target,
            kind=NotificationKind.MAIN,
        )
        session.add(existing)
        await session.flush()
    elif existing.status is NotificationStatus.CANCELLED:
        existing.status = NotificationStatus.PENDING
        existing.next_attempt_at_utc = None
    elif existing.status is not NotificationStatus.PENDING:
        raise DomainError("snooze target notification is no longer pending")
    occurrence.status = OccurrenceStatus.PENDING
    return await _record_action(
        session,
        source,
        occurrence,
        ReactionAction.SNOOZE,
        changed=True,
        cancelled=cancelled,
        scheduled_notification_id=existing.id,
        target=target,
    )


async def preview_move_tomorrow(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    *,
    now_utc: dt.datetime,
) -> MovePreview:
    _require_aware(now_utc)
    source, occurrence, _entry, replay = await _lock_source(
        session,
        user_id,
        source_notification_id,
        ReactionAction.MOVE,
        reacted_at_utc=now_utc,
    )
    if replay is not None:
        raise DomainError("notification already handled")
    settings = await session.get(UserSettings, user_id)
    if settings is None:
        raise DomainError("user settings not found")
    await acknowledge_occurrence(
        session,
        user_id,
        occurrence.id,
        reacted_at_utc=now_utc,
        clear_context=False,
    )
    zone = ZoneInfo(settings.tz)
    local_time = (
        occurrence.planned_at_utc.astimezone(zone)
        .timetz()
        .replace(tzinfo=None, second=0, microsecond=0)
    )
    current_local_date = occurrence.planned_at_utc.astimezone(zone).date()
    target_date = max(now_utc.astimezone(zone).date(), current_local_date) + dt.timedelta(days=1)
    target_wall = dt.datetime.combine(target_date, local_time)
    target: dt.datetime | None = None
    for hour_offset in range(25):
        candidate = resolve_wall_time(
            target_wall + dt.timedelta(hours=hour_offset),
            settings.tz,
        )
        collision = await session.scalar(
            select(Occurrence.id).where(
                Occurrence.entry_id == occurrence.entry_id,
                Occurrence.planned_at_utc == candidate,
            )
        )
        if collision is None:
            target = candidate
            break
    if target is None:
        raise DomainError("no free move target found in the next day")
    return MovePreview(source.id, occurrence.id, target, target.astimezone(zone), settings.tz)


async def move_from_notification(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    target_at_utc: dt.datetime,
    *,
    reacted_at_utc: dt.datetime,
    defaults: ReminderDefaults,
) -> ReactionResult:
    _require_aware(target_at_utc)
    source, occurrence, entry, replay = await _lock_source(
        session,
        user_id,
        source_notification_id,
        ReactionAction.MOVE,
        reacted_at_utc=reacted_at_utc,
    )
    if replay is not None:
        if replay.target_at_utc != target_at_utc:
            raise DomainError("notification already handled")
        return replay
    _require_actionable(occurrence)
    if target_at_utc == occurrence.planned_at_utc:
        raise DomainError("target time must differ from the current time")
    if target_at_utc <= reacted_at_utc:
        raise DomainError("target time must be in the future")
    collision = await session.scalar(
        select(Occurrence.id).where(
            Occurrence.entry_id == entry.id,
            Occurrence.planned_at_utc == target_at_utc,
        )
    )
    if collision is not None:
        raise DomainError("another occurrence already exists at target time")
    acknowledged = await acknowledge_occurrence(
        session, user_id, occurrence.id, reacted_at_utc=reacted_at_utc
    )
    cancelled = acknowledged.cancelled + await _cancel_pending(session, occurrence.id)
    occurrence.status = OccurrenceStatus.MOVED
    occurrence.moved_count += 1
    settings = await session.get(UserSettings, user_id)
    if settings is None:
        raise DomainError("user settings not found")
    if entry.kind in {EntryKind.EVENT, EntryKind.TASK}:
        entry.tz = settings.tz
        entry.local_time = (
            target_at_utc.astimezone(ZoneInfo(settings.tz)).timetz().replace(tzinfo=None)
        )
    if entry.kind is EntryKind.EVENT:
        entry.start_at_utc = target_at_utc
    elif entry.kind is EntryKind.TASK:
        entry.due_at_utc = target_at_utc
    successor = Occurrence(
        entry_id=entry.id,
        user_id=user_id,
        planned_at_utc=target_at_utc,
        moved_count=occurrence.moved_count,
    )
    session.add(successor)
    await session.flush()
    await schedule_notifications(
        session,
        now_utc=reacted_at_utc,
        defaults=defaults,
        occurrence_ids=(successor.id,),
    )
    return await _record_action(
        session,
        source,
        occurrence,
        ReactionAction.MOVE,
        changed=True,
        cancelled=cancelled,
        successor_occurrence_id=successor.id,
        target=target_at_utc,
    )


async def _lock_source(
    session: AsyncSession,
    user_id: int,
    source_notification_id: int,
    action: ReactionAction,
    *,
    reacted_at_utc: dt.datetime,
) -> tuple[Notification, Occurrence, Entry, ReactionResult | None]:
    _require_aware(reacted_at_utc)
    snapshot = await session.execute(
        select(Notification.occurrence_id).where(
            Notification.id == source_notification_id,
            Notification.user_id == user_id,
        )
    )
    occurrence_id = snapshot.scalar_one_or_none()
    if occurrence_id is None:
        raise DomainError("notification not found")
    await lock_user_context(session, user_id)
    settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id).with_for_update()
    )
    if settings is None:
        raise DomainError("user settings not found")
    await lock_occurrence_action(session, occurrence_id)
    occurrence = await session.scalar(
        select(Occurrence)
        .where(Occurrence.id == occurrence_id, Occurrence.user_id == user_id)
        .with_for_update()
    )
    source = await session.scalar(
        select(Notification)
        .where(
            Notification.id == source_notification_id,
            Notification.user_id == user_id,
            Notification.occurrence_id == occurrence_id,
        )
        .with_for_update()
    )
    if occurrence is None or source is None:
        raise DomainError("notification not found")
    entry = await session.get(Entry, occurrence.entry_id)
    if entry is None:
        raise DomainError("entry not found")
    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.user_id == user_id,
            AuditLog.entity == "notification_action",
            AuditLog.entity_id == source_notification_id,
        )
    )
    if audit is not None:
        stored_action = ReactionAction(str(audit.payload["intent"]))
        if stored_action is not action:
            raise DomainError("notification already handled")
        return source, occurrence, entry, _result_from_audit(audit)
    context = await session.scalar(
        select(ActiveContext).where(ActiveContext.user_id == user_id).with_for_update()
    )
    _require_reactable_source(source, context, reacted_at_utc=reacted_at_utc)
    return source, occurrence, entry, None


async def _cancel_pending(session: AsyncSession, occurrence_id: int) -> int:
    result = await session.execute(
        update(Notification)
        .where(
            Notification.occurrence_id == occurrence_id,
            Notification.status == NotificationStatus.PENDING,
        )
        .values(status=NotificationStatus.CANCELLED, next_attempt_at_utc=None)
    )
    return int(result.rowcount or 0)


def _require_actionable(occurrence: Occurrence) -> None:
    if occurrence.status not in {OccurrenceStatus.PENDING, OccurrenceStatus.MISSED}:
        raise DomainError(f"occurrence in status {occurrence.status.value} cannot be changed")


def _require_reactable_source(
    source: Notification,
    context: ActiveContext | None,
    *,
    reacted_at_utc: dt.datetime,
) -> None:
    if source.status is NotificationStatus.SENT:
        return
    if (
        source.status is NotificationStatus.PENDING
        and context is not None
        and context.notification_id == source.id
        and context.expires_at_utc > reacted_at_utc
    ):
        return
    raise DomainError("notification has not been sent")


async def _record_action(
    session: AsyncSession,
    source: Notification,
    occurrence: Occurrence,
    action: ReactionAction,
    *,
    changed: bool,
    cancelled: int = 0,
    scheduled_notification_id: int | None = None,
    successor_occurrence_id: int | None = None,
    target: dt.datetime | None = None,
) -> ReactionResult:
    payload = {
        "intent": action.value,
        "occurrence_id": occurrence.id,
        "status": occurrence.status.value,
        "changed": changed,
        "cancelled_notifications": cancelled,
        "scheduled_notification_id": scheduled_notification_id,
        "successor_occurrence_id": successor_occurrence_id,
        "target_at_utc": target.isoformat() if target else None,
        "moved_count": occurrence.moved_count,
    }
    session.add(
        AuditLog(
            user_id=occurrence.user_id,
            entity="notification_action",
            entity_id=source.id,
            action=AuditAction.UPDATE,
            payload=payload,
        )
    )
    await session.flush()
    return ReactionResult(
        source_notification_id=source.id,
        occurrence_id=occurrence.id,
        action=action,
        changed=changed,
        status=occurrence.status,
        cancelled_notifications=cancelled,
        scheduled_notification_id=scheduled_notification_id,
        successor_occurrence_id=successor_occurrence_id,
        target_at_utc=target,
        moved_count=occurrence.moved_count,
    )


def _result_from_audit(audit: AuditLog) -> ReactionResult:
    payload = audit.payload
    target = (
        dt.datetime.fromisoformat(payload["target_at_utc"]) if payload["target_at_utc"] else None
    )
    return ReactionResult(
        source_notification_id=int(audit.entity_id or 0),
        occurrence_id=int(payload["occurrence_id"]),
        action=ReactionAction(str(payload["intent"])),
        changed=False,
        status=OccurrenceStatus(str(payload["status"])),
        cancelled_notifications=int(payload["cancelled_notifications"]),
        scheduled_notification_id=payload["scheduled_notification_id"],
        successor_occurrence_id=payload["successor_occurrence_id"],
        target_at_utc=target,
        moved_count=int(payload["moved_count"]),
    )


def _require_aware(value: dt.datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


__all__ = [
    "MovePreview",
    "ReactionAction",
    "ReactionResult",
    "apply_notification_action",
    "complete_from_notification",
    "move_from_notification",
    "preview_move_tomorrow",
    "skip_from_notification",
    "snooze_from_notification",
]
