"""Подтверждённая смена таймзоны и разбор будущих разовых записей.

Модуль не знает ни про HTTP, ни про Telegram. Незавершённый review хранится
устойчиво в ``tz_changes`` + ``audit_log``, поэтому переживает рестарт обоих
адаптеров без новой таблицы.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.enums import (
    AuditAction,
    EntryKind,
    EntryStatus,
    NotificationStatus,
    OccurrenceStatus,
)
from seshat.db.models import AuditLog, Entry, Notification, Occurrence, TzChange, UserSettings
from seshat.domain.scheduling import (
    MaterializeResult,
    ReminderDefaults,
    ScheduleResult,
    materialize_occurrences,
    schedule_notifications,
)
from seshat.domain.users import DomainError, validate_tz


class TimezoneConflictError(DomainError):
    """Preview устарел или другой переезд ещё не разобран."""


class TimezoneReviewDecision(StrEnum):
    KEEP_ABSOLUTE = "keep_absolute"
    KEEP_LOCAL = "keep_local"


class TimezoneChangePreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    confirmation_id: uuid.UUID
    tz_from: str
    tz_to: str
    now_from: dt.datetime
    now_to: dt.datetime
    routine_count: int
    review_count: int


class TimezoneReviewItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_id: int
    kind: EntryKind
    title: str
    moment_field: str
    moment_at_utc: dt.datetime
    keep_absolute_local: dt.datetime
    keep_local_at_utc: dt.datetime
    keep_local_local: dt.datetime


class TimezoneChangeResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    change: TzChange
    applied: bool
    routines_rebased: int
    review_total: int
    review_remaining: int
    next_review: TimezoneReviewItem | None = None


class TimezoneReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    change_id: int
    entry_id: int
    decision: TimezoneReviewDecision
    applied: bool
    review_remaining: int
    next_review: TimezoneReviewItem | None = None


class TimezoneScheduleResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    materialized: MaterializeResult
    scheduled: ScheduleResult


def _require_aware(moment: dt.datetime) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now_utc должен быть tz-aware")
    return moment.astimezone(dt.UTC)


def _wall_to_utc(day: dt.date, local_time: dt.time, tz: str) -> dt.datetime:
    """Локальное время → UTC с устойчивой DST-политикой.

    Неоднозначное осеннее время берёт первый экземпляр (fold=0). Несуществующее
    весеннее время проходит round-trip и сдвигается вперёд на размер разрыва.
    """
    zone = ZoneInfo(tz)
    wall = dt.datetime.combine(day, local_time).replace(tzinfo=zone, fold=0)
    return wall.astimezone(dt.UTC).astimezone(zone).astimezone(dt.UTC)


def _entry_moment(entry: Entry) -> tuple[str, dt.datetime] | None:
    if entry.kind is EntryKind.EVENT and entry.start_at_utc is not None:
        return "start_at_utc", entry.start_at_utc.astimezone(dt.UTC)
    if entry.kind is EntryKind.TASK and entry.due_at_utc is not None:
        return "due_at_utc", entry.due_at_utc.astimezone(dt.UTC)
    return None


def _same_local_moment(
    entry: Entry, moment: dt.datetime, current_tz: str, new_tz: str
) -> dt.datetime:
    old_local = moment.astimezone(ZoneInfo(current_tz))
    # KEEP_ABSOLUTE намеренно оставляет entry.tz исторической. При следующем
    # переезде ориентируемся на текущее отображаемое местное время, а не на
    # устаревшую зону создания записи.
    local_time = (
        entry.local_time
        if entry.tz == current_tz and entry.local_time is not None
        else old_local.timetz().replace(tzinfo=None)
    )
    return _wall_to_utc(old_local.date(), local_time, new_tz)


async def _active_entries(session: AsyncSession, user_id: int) -> list[Entry]:
    return list(
        (
            await session.scalars(
                select(Entry)
                .where(
                    Entry.user_id == user_id,
                    Entry.status == EntryStatus.ACTIVE,
                    Entry.deleted_at.is_(None),
                )
                .order_by(Entry.id)
            )
        ).all()
    )


async def preview_timezone_change(
    session: AsyncSession,
    user_id: int,
    new_tz: str,
    *,
    now_utc: dt.datetime,
    confirmation_id: uuid.UUID | None = None,
) -> TimezoneChangePreview:
    now = _require_aware(now_utc)
    validate_tz(new_tz)
    settings = await session.get(UserSettings, user_id)
    if settings is None:
        raise DomainError("настройки пользователя не найдены")
    if settings.tz == new_tz:
        raise DomainError("новая таймзона совпадает с текущей")

    entries = await _active_entries(session, user_id)
    review_count = sum(
        1 for entry in entries if (moment := _entry_moment(entry)) is not None and moment[1] > now
    )
    return TimezoneChangePreview(
        confirmation_id=confirmation_id or uuid.uuid4(),
        tz_from=settings.tz,
        tz_to=new_tz,
        now_from=now.astimezone(ZoneInfo(settings.tz)),
        now_to=now.astimezone(ZoneInfo(new_tz)),
        routine_count=sum(entry.kind is EntryKind.ROUTINE for entry in entries),
        review_count=review_count,
    )


async def _rebase_occurrences(
    session: AsyncSession,
    entry: Entry,
    *,
    old_tz: str,
    new_tz: str,
    now_utc: dt.datetime,
) -> None:
    """Переносит будущую материализацию и pending-уведомления на новый UTC.

    Отправленные уведомления намеренно не выбираются и не изменяются. Смещения
    pending-напоминаний относительно occurrence сохраняются; планировщик затем
    может дополнить горизонт новыми строками обычным идемпотентным проходом.
    """
    occurrences = list(
        (
            await session.scalars(
                select(Occurrence)
                .where(
                    Occurrence.entry_id == entry.id,
                    Occurrence.planned_at_utc > now_utc,
                    Occurrence.status == OccurrenceStatus.PENDING,
                )
                .order_by(Occurrence.planned_at_utc)
                .with_for_update()
            )
        ).all()
    )
    for occurrence in occurrences:
        old_moment = occurrence.planned_at_utc.astimezone(dt.UTC)
        old_local = old_moment.astimezone(ZoneInfo(old_tz))
        local_time = (
            entry.local_time
            if entry.tz == old_tz and entry.local_time is not None
            else old_local.timetz().replace(tzinfo=None)
        )
        new_moment = _wall_to_utc(old_local.date(), local_time, new_tz)
        pending = list(
            (
                await session.scalars(
                    select(Notification)
                    .where(
                        Notification.occurrence_id == occurrence.id,
                        Notification.status == NotificationStatus.PENDING,
                    )
                    .with_for_update()
                )
            ).all()
        )
        for notification in pending:
            offset = old_moment - notification.fire_at_utc.astimezone(dt.UTC)
            notification.fire_at_utc = new_moment - offset
        occurrence.planned_at_utc = new_moment


async def _rebase_routine(
    session: AsyncSession,
    entry: Entry,
    *,
    new_tz: str,
    now_utc: dt.datetime,
) -> None:
    old_tz = entry.tz
    await _rebase_occurrences(
        session,
        entry,
        old_tz=old_tz,
        new_tz=new_tz,
        now_utc=now_utc,
    )
    if entry.start_at_utc is not None:
        old_local = entry.start_at_utc.astimezone(ZoneInfo(old_tz))
        local_time = entry.local_time or old_local.timetz().replace(tzinfo=None)
        entry.start_at_utc = _wall_to_utc(old_local.date(), local_time, new_tz)
    entry.tz = new_tz


async def _change_audit(session: AsyncSession, change_id: int) -> AuditLog:
    audit = (
        await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity == "timezone_change",
                AuditLog.entity_id == change_id,
                AuditLog.action == AuditAction.TZ_CHANGE,
            )
            .order_by(AuditLog.id)
            .limit(1)
        )
    ).one_or_none()
    if audit is None:
        raise DomainError("журнал смены таймзоны не найден")
    return audit


async def _decision_map(session: AsyncSession, change_id: int) -> dict[int, AuditLog]:
    rows = list(
        (
            await session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.entity == "timezone_review",
                    AuditLog.action == AuditAction.UPDATE,
                    AuditLog.payload["tz_change_id"].as_string() == str(change_id),
                )
                .order_by(AuditLog.id)
            )
        ).all()
    )
    return {row.entity_id: row for row in rows if row.entity_id is not None}


def _review_item(entry: Entry, current_tz: str, new_tz: str) -> TimezoneReviewItem:
    field_and_moment = _entry_moment(entry)
    if field_and_moment is None:
        raise DomainError("у записи нет будущего момента для разбора")
    field, moment = field_and_moment
    keep_local_at = _same_local_moment(entry, moment, current_tz, new_tz)
    return TimezoneReviewItem(
        entry_id=entry.id,
        kind=entry.kind,
        title=entry.title,
        moment_field=field,
        moment_at_utc=moment,
        keep_absolute_local=moment.astimezone(ZoneInfo(new_tz)),
        keep_local_at_utc=keep_local_at,
        keep_local_local=keep_local_at.astimezone(ZoneInfo(new_tz)),
    )


async def list_timezone_reviews(
    session: AsyncSession,
    user_id: int,
    change_id: int,
) -> list[TimezoneReviewItem]:
    change = await session.get(TzChange, change_id)
    if change is None or change.user_id != user_id:
        raise DomainError("смена таймзоны не найдена")
    audit = await _change_audit(session, change_id)
    snapshot = [int(value) for value in (audit.payload or {}).get("review_entry_ids", [])]
    decisions = await _decision_map(session, change_id)
    remaining_ids = [entry_id for entry_id in snapshot if entry_id not in decisions]
    if not remaining_ids:
        if not change.entries_reviewed:
            change.entries_reviewed = True
            await session.flush()
        return []
    entries = {
        entry.id: entry
        for entry in (
            await session.scalars(
                select(Entry).where(Entry.user_id == user_id, Entry.id.in_(remaining_ids))
            )
        ).all()
    }
    return [
        _review_item(entries[entry_id], change.tz_from, change.tz_to) for entry_id in remaining_ids
    ]


async def find_pending_timezone_change(session: AsyncSession, user_id: int) -> TzChange | None:
    """Возвращает незавершённый review для восстановления после рестарта."""
    return (
        await session.scalars(
            select(TzChange)
            .where(
                TzChange.user_id == user_id,
                TzChange.entries_reviewed.is_(False),
            )
            .order_by(TzChange.id)
            .limit(1)
        )
    ).one_or_none()


async def rebuild_timezone_horizon(
    session: AsyncSession,
    user_id: int,
    *,
    now_utc: dt.datetime,
    horizon_days: int,
    defaults: ReminderDefaults,
) -> TimezoneScheduleResult:
    """Немедленно дополняет горизонт после подтверждения или решения review."""
    now = _require_aware(now_utc)
    materialized = await materialize_occurrences(
        session,
        now_utc=now,
        horizon_days=horizon_days,
        user_id=user_id,
    )
    scheduled = await schedule_notifications(
        session,
        now_utc=now,
        defaults=defaults,
        occurrence_ids=materialized.created_occurrence_ids,
    )
    return TimezoneScheduleResult(materialized=materialized, scheduled=scheduled)


async def _result_for_change(
    session: AsyncSession,
    change: TzChange,
    *,
    applied: bool,
    routines_rebased: int,
) -> TimezoneChangeResult:
    audit = await _change_audit(session, change.id)
    total = len((audit.payload or {}).get("review_entry_ids", []))
    remaining = await list_timezone_reviews(session, change.user_id, change.id)
    return TimezoneChangeResult(
        change=change,
        applied=applied,
        routines_rebased=routines_rebased,
        review_total=total,
        review_remaining=len(remaining),
        next_review=remaining[0] if remaining else None,
    )


async def confirm_timezone_change(
    session: AsyncSession,
    user_id: int,
    new_tz: str,
    *,
    expected_tz_from: str,
    confirmation_id: uuid.UUID,
    now_utc: dt.datetime,
) -> TimezoneChangeResult:
    now = _require_aware(now_utc)
    validate_tz(new_tz)
    confirmation = str(confirmation_id)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"seshat:timezone:{user_id}:{confirmation}"},
    )
    existing = (
        await session.scalars(
            select(AuditLog)
            .where(
                AuditLog.user_id == user_id,
                AuditLog.entity == "timezone_change",
                AuditLog.action == AuditAction.TZ_CHANGE,
                AuditLog.payload["confirmation_id"].as_string() == confirmation,
            )
            .order_by(AuditLog.id)
            .limit(1)
        )
    ).one_or_none()
    if existing is not None:
        payload = existing.payload or {}
        if payload.get("from") != expected_tz_from or payload.get("to") != new_tz:
            raise TimezoneConflictError("confirmation_id уже использован для другой смены таймзоны")
        if existing.entity_id is None:
            raise DomainError("журнал смены не содержит идентификатор")
        change = await session.get(TzChange, existing.entity_id)
        if change is None:
            raise DomainError("подтверждённая смена таймзоны не найдена")
        return await _result_for_change(
            session, change, applied=False, routines_rebased=int(payload.get("routines", 0))
        )

    settings = (
        await session.scalars(
            select(UserSettings).where(UserSettings.user_id == user_id).with_for_update()
        )
    ).one_or_none()
    if settings is None:
        raise DomainError("настройки пользователя не найдены")
    if settings.tz != expected_tz_from:
        raise TimezoneConflictError("карточка устарела: текущая таймзона уже изменилась")
    if settings.tz == new_tz:
        raise DomainError("новая таймзона совпадает с текущей")
    pending = (
        await session.scalars(
            select(TzChange).where(
                TzChange.user_id == user_id,
                TzChange.entries_reviewed.is_(False),
            )
        )
    ).first()
    if pending is not None:
        raise TimezoneConflictError(
            f"сначала заверши разбор записей для смены таймзоны #{pending.id}"
        )

    entries = await _active_entries(session, user_id)
    review_ids = [
        entry.id
        for entry in entries
        if (moment := _entry_moment(entry)) is not None and moment[1] > now
    ]
    routines = [entry for entry in entries if entry.kind is EntryKind.ROUTINE]
    change = TzChange(
        user_id=user_id,
        tz_from=settings.tz,
        tz_to=new_tz,
        entries_reviewed=not review_ids,
    )
    session.add(change)
    await session.flush()
    for routine in routines:
        await _rebase_routine(session, routine, new_tz=new_tz, now_utc=now)
    settings.tz = new_tz
    session.add(
        AuditLog(
            user_id=user_id,
            entity="timezone_change",
            entity_id=change.id,
            action=AuditAction.TZ_CHANGE,
            payload={
                "confirmation_id": confirmation,
                "from": expected_tz_from,
                "to": new_tz,
                "routines": len(routines),
                "review_entry_ids": review_ids,
            },
        )
    )
    await session.flush()
    return await _result_for_change(session, change, applied=True, routines_rebased=len(routines))


async def review_timezone_entry(
    session: AsyncSession,
    user_id: int,
    change_id: int,
    entry_id: int,
    decision: TimezoneReviewDecision,
    *,
    now_utc: dt.datetime,
) -> TimezoneReviewResult:
    now = _require_aware(now_utc)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"seshat:timezone-review:{change_id}:{entry_id}"},
    )
    change = (
        await session.scalars(
            select(TzChange)
            .where(TzChange.id == change_id, TzChange.user_id == user_id)
            .with_for_update()
        )
    ).one_or_none()
    if change is None:
        raise DomainError("смена таймзоны не найдена")
    audit = await _change_audit(session, change_id)
    snapshot = [int(value) for value in (audit.payload or {}).get("review_entry_ids", [])]
    if entry_id not in snapshot:
        raise DomainError("запись не входит в разбор этой смены таймзоны")
    existing = (await _decision_map(session, change_id)).get(entry_id)
    if existing is not None:
        old_decision = (existing.payload or {}).get("decision")
        if old_decision != decision.value:
            raise TimezoneConflictError("решение по записи уже принято")
        remaining = await list_timezone_reviews(session, user_id, change_id)
        return TimezoneReviewResult(
            change_id=change_id,
            entry_id=entry_id,
            decision=decision,
            applied=False,
            review_remaining=len(remaining),
            next_review=remaining[0] if remaining else None,
        )

    entry = await session.get(Entry, entry_id, with_for_update=True)
    if entry is None or entry.user_id != user_id:
        raise DomainError("запись для разбора не найдена")
    item = _review_item(entry, change.tz_from, change.tz_to)
    before = item.moment_at_utc
    after = before
    if decision is TimezoneReviewDecision.KEEP_LOCAL:
        after = item.keep_local_at_utc
        await _rebase_occurrences(
            session,
            entry,
            old_tz=change.tz_from,
            new_tz=change.tz_to,
            now_utc=now,
        )
        if item.moment_field == "start_at_utc":
            entry.start_at_utc = after
        else:
            entry.due_at_utc = after
        entry.tz = change.tz_to
        entry.local_time = item.keep_local_local.timetz().replace(tzinfo=None)

    session.add(
        AuditLog(
            user_id=user_id,
            entity="timezone_review",
            entity_id=entry_id,
            action=AuditAction.UPDATE,
            payload={
                "tz_change_id": str(change_id),
                "decision": decision.value,
                "before_utc": before.isoformat(),
                "after_utc": after.isoformat(),
            },
        )
    )
    await session.flush()
    remaining = await list_timezone_reviews(session, user_id, change_id)
    return TimezoneReviewResult(
        change_id=change_id,
        entry_id=entry_id,
        decision=decision,
        applied=True,
        review_remaining=len(remaining),
        next_review=remaining[0] if remaining else None,
    )


__all__ = [
    "TimezoneChangePreview",
    "TimezoneChangeResult",
    "TimezoneConflictError",
    "TimezoneReviewDecision",
    "TimezoneReviewItem",
    "TimezoneReviewResult",
    "TimezoneScheduleResult",
    "confirm_timezone_change",
    "find_pending_timezone_change",
    "list_timezone_reviews",
    "preview_timezone_change",
    "rebuild_timezone_horizon",
    "review_timezone_entry",
]
