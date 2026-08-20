"""Materialization of entries and durable notification scheduling.

All recurrence calculations happen in the entry's local timezone.  The
database remains the source of truth: repeated reconciliation is safe because
both occurrences and notifications are inserted through PostgreSQL upserts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.enums import (
    AuditAction,
    EntryKind,
    EntryStatus,
    NotificationKind,
    NotificationStatus,
    OccurrenceStatus,
)
from seshat.db.models import ActiveContext, AuditLog, Entry, Notification, Occurrence, UserSettings
from seshat.domain.locks import lock_occurrence_action, lock_user_context
from seshat.domain.users import DomainError, validate_tz


@dataclass(frozen=True)
class ReminderDefaults:
    """Configurable preliminary reminders; MAIN is always implicit."""

    event_pre_min: tuple[int, ...] = (15,)
    task_pre_min: tuple[int, ...] = (120,)
    task_morning_local: dt.time = dt.time(8, 0)
    routine_pre_min: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for values in (self.event_pre_min, self.task_pre_min, self.routine_pre_min):
            if any(value <= 0 for value in values):
                raise ValueError("default reminder offsets must be positive")


@dataclass(frozen=True)
class MaterializeResult:
    created_occurrence_ids: tuple[int, ...]
    existing_count: int


@dataclass(frozen=True)
class ScheduleResult:
    created_notification_ids: tuple[int, ...]
    reactivated_notification_ids: tuple[int, ...]
    existing_count: int


@dataclass(frozen=True)
class OccurrenceActionResult:
    occurrence_id: int
    changed: bool


def _require_utc(moment: dt.datetime, *, name: str) -> dt.datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return moment.astimezone(dt.UTC)


def resolve_wall_time(moment_local: dt.datetime, tz: str) -> dt.datetime:
    """Resolve a naive wall-clock value deterministically and return UTC.

    During the autumn overlap ``fold=0`` selects the first occurrence.  During
    the spring gap the wall time is shifted forward by the size of the gap, so
    a daily routine is not silently omitted (02:30 becomes 03:30).
    """
    if moment_local.tzinfo is not None and moment_local.utcoffset() is not None:
        raise ValueError("moment_local must be a naive wall-clock datetime")
    validate_tz(tz)
    zone = ZoneInfo(tz)

    candidates: list[tuple[dt.datetime, dt.datetime]] = []
    for fold in (0, 1):
        utc_value = moment_local.replace(tzinfo=zone, fold=fold).astimezone(dt.UTC)
        roundtrip = utc_value.astimezone(zone).replace(tzinfo=None)
        candidates.append((roundtrip, utc_value))

    valid = [utc_value for roundtrip, utc_value in candidates if roundtrip == moment_local]
    if valid:
        # fold=0 was appended first and is the documented overlap policy.
        return valid[0]

    shifted_forward = [item for item in candidates if item[0] > moment_local]
    if not shifted_forward:  # pragma: no cover - defensive for exotic tzdata
        raise DomainError(f"cannot resolve local time {moment_local!s} in {tz}")
    return min(shifted_forward, key=lambda item: item[0])[1]


def _entry_anchor(entry: Entry) -> dt.datetime | None:
    return entry.start_at_utc or entry.due_at_utc


def _routine_candidates(
    entry: Entry,
    *,
    window_start_utc: dt.datetime,
    window_end_utc: dt.datetime,
) -> list[dt.datetime]:
    anchor_utc = _entry_anchor(entry)
    if anchor_utc is None or entry.local_time is None or entry.rrule is None:
        raise DomainError(f"routine entry {entry.id} has incomplete recurrence fields")

    zone = ZoneInfo(entry.tz)
    anchor_date = anchor_utc.astimezone(zone).date()
    anchor_local = dt.datetime.combine(anchor_date, entry.local_time)
    rule = rrulestr(entry.rrule, dtstart=anchor_local)

    # Local and UTC window edges can differ around DST.  Generate with a
    # one-day margin, then apply the authoritative UTC filter below.
    local_start = window_start_utc.astimezone(zone).replace(tzinfo=None) - dt.timedelta(days=1)
    local_end = window_end_utc.astimezone(zone).replace(tzinfo=None) + dt.timedelta(days=1)
    result: list[dt.datetime] = []
    for candidate_local in rule.between(local_start, local_end, inc=True):
        candidate_utc = resolve_wall_time(candidate_local.replace(tzinfo=None), entry.tz)
        if window_start_utc <= candidate_utc <= window_end_utc:
            result.append(candidate_utc)
    return result


async def materialize_occurrences(
    session: AsyncSession,
    *,
    now_utc: dt.datetime,
    horizon_days: int,
    lookback_minutes: int = 30,
    user_id: int | None = None,
    entry_id: int | None = None,
) -> MaterializeResult:
    """Expand active entries into a bounded, restart-safe occurrence window."""
    now_utc = _require_utc(now_utc, name="now_utc")
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    if lookback_minutes < 0:
        raise ValueError("lookback_minutes cannot be negative")

    base_filters = (
        Entry.status == EntryStatus.ACTIVE,
        Entry.deleted_at.is_(None),
    )
    users_query = select(Entry.user_id).where(*base_filters).distinct().order_by(Entry.user_id)
    if user_id is not None:
        users_query = users_query.where(Entry.user_id == user_id)
    if entry_id is not None:
        users_query = users_query.where(Entry.id == entry_id)
    user_ids = tuple((await session.execute(users_query)).scalars().all())

    entries: list[Entry] = []
    for locked_user_id in user_ids:
        # Общая граница с create_planned_entry и confirm_timezone_change:
        # пока планировщик читает tz и вставляет горизонт, переезд ждёт. После
        # переезда SELECT выполняется заново и уже видит новую зону.
        await session.execute(
            select(UserSettings).where(UserSettings.user_id == locked_user_id).with_for_update()
        )
        query = select(Entry).where(*base_filters, Entry.user_id == locked_user_id)
        if entry_id is not None:
            query = query.where(Entry.id == entry_id)
        entries.extend(
            (
                await session.execute(
                    query.order_by(Entry.id).execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )

    window_start = now_utc - dt.timedelta(minutes=lookback_minutes)
    window_end = now_utc + dt.timedelta(days=horizon_days)
    values: list[dict[str, object]] = []
    seen: set[tuple[int, dt.datetime]] = set()
    for entry in entries:
        if entry.kind is EntryKind.ROUTINE:
            moments = _routine_candidates(
                entry,
                window_start_utc=window_start,
                window_end_utc=window_end,
            )
        else:
            anchor = _entry_anchor(entry)
            moments = (
                [anchor] if anchor is not None and window_start <= anchor <= window_end else []
            )

        for planned_at in moments:
            key = (entry.id, planned_at)
            if key in seen:
                continue
            seen.add(key)
            values.append(
                {
                    "entry_id": entry.id,
                    "user_id": entry.user_id,
                    "planned_at_utc": planned_at,
                    "status": OccurrenceStatus.PENDING,
                    "moved_count": 0,
                }
            )

    if not values:
        return MaterializeResult(created_occurrence_ids=(), existing_count=0)

    statement = (
        pg_insert(Occurrence)
        .values(values)
        .on_conflict_do_nothing(index_elements=["entry_id", "planned_at_utc"])
        .returning(Occurrence.id)
    )
    created_ids = tuple((await session.execute(statement)).scalars().all())
    return MaterializeResult(
        created_occurrence_ids=created_ids,
        existing_count=len(values) - len(created_ids),
    )


def _default_offsets(entry: Entry, defaults: ReminderDefaults) -> tuple[int, ...]:
    if entry.kind is EntryKind.EVENT:
        return defaults.event_pre_min
    if entry.kind is EntryKind.TASK:
        return defaults.task_pre_min
    return defaults.routine_pre_min


def _notification_specs(
    occurrence: Occurrence,
    entry: Entry,
    defaults: ReminderDefaults,
) -> set[tuple[NotificationKind, dt.datetime]]:
    planned_at = _require_utc(occurrence.planned_at_utc, name="planned_at_utc")
    specs = {(NotificationKind.MAIN, planned_at)}

    # [] means "not specified" and applies config defaults.  [0] is the
    # explicit main-only representation supported by the existing schema.
    configured = tuple(entry.reminders_min_before or ())
    offsets = tuple(value for value in configured if value > 0)
    if not configured:
        offsets = _default_offsets(entry, defaults)
    for offset in offsets:
        specs.add((NotificationKind.PRE, planned_at - dt.timedelta(minutes=offset)))

    if entry.kind is EntryKind.TASK and not configured:
        zone = ZoneInfo(entry.tz)
        due_date = planned_at.astimezone(zone).date()
        morning = resolve_wall_time(
            dt.datetime.combine(due_date, defaults.task_morning_local), entry.tz
        )
        if morning < planned_at:
            specs.add((NotificationKind.PRE, morning))
    return specs


async def schedule_notifications(
    session: AsyncSession,
    *,
    now_utc: dt.datetime,
    defaults: ReminderDefaults,
    occurrence_ids: Collection[int] | None = None,
) -> ScheduleResult:
    """Create raw notification rows; quiet-hour policy is applied at delivery."""
    _require_utc(now_utc, name="now_utc")
    if occurrence_ids is not None and not occurrence_ids:
        return ScheduleResult((), (), 0)

    query = (
        select(Occurrence, Entry)
        .join(Entry, Entry.id == Occurrence.entry_id)
        .where(
            Occurrence.status == OccurrenceStatus.PENDING,
            Entry.status == EntryStatus.ACTIVE,
            Entry.deleted_at.is_(None),
        )
        .order_by(Occurrence.id)
    )
    if occurrence_ids is not None:
        query = query.where(Occurrence.id.in_(occurrence_ids))
    rows = (await session.execute(query)).all()

    # A snooze deliberately replaces the occurrence's standard PRE/MAIN plan
    # with one durable signal.  Reconciliation must not resurrect the cancelled
    # standard rows after a restart.
    row_ids = {occurrence.id for occurrence, _entry in rows}
    snoozed_ids: set[int] = set()
    if row_ids:
        snoozed_ids = set(
            (
                await session.scalars(
                    select(Notification.occurrence_id)
                    .join(AuditLog, AuditLog.entity_id == Notification.id)
                    .where(
                        Notification.occurrence_id.in_(row_ids),
                        AuditLog.entity == "notification_action",
                        AuditLog.payload["intent"].astext == "snooze",
                    )
                    .distinct()
                )
            ).all()
        )

    values: list[dict[str, object]] = []
    keys: list[tuple[int, dt.datetime, NotificationKind]] = []
    for occurrence, entry in rows:
        if occurrence.id in snoozed_ids:
            continue
        for kind, fire_at in sorted(
            _notification_specs(occurrence, entry, defaults),
            key=lambda item: (item[1], item[0].value),
        ):
            values.append(
                {
                    "occurrence_id": occurrence.id,
                    "user_id": occurrence.user_id,
                    "fire_at_utc": fire_at,
                    "kind": kind,
                    "status": NotificationStatus.PENDING,
                    "silent": False,
                }
            )
            keys.append((occurrence.id, fire_at, kind))

    if not values:
        return ScheduleResult((), (), 0)

    existing_rows = (
        (
            await session.execute(
                select(Notification).where(Notification.occurrence_id.in_({key[0] for key in keys}))
            )
        )
        .scalars()
        .all()
    )
    existing = {
        (item.occurrence_id, item.fire_at_utc, item.kind): (item.id, item.status)
        for item in existing_rows
    }
    existing_objects = {
        (item.occurrence_id, item.fire_at_utc, item.kind): item for item in existing_rows
    }

    insert_statement = pg_insert(Notification).values(values)
    statement = insert_statement.on_conflict_do_update(
        index_elements=["occurrence_id", "fire_at_utc", "kind"],
        set_={
            "status": NotificationStatus.PENDING,
            "silent": False,
            "sent_at_utc": None,
            "telegram_message_id": None,
        },
        where=Notification.status == NotificationStatus.CANCELLED,
    ).returning(
        Notification.id,
        Notification.occurrence_id,
        Notification.fire_at_utc,
        Notification.kind,
    )
    affected = (await session.execute(statement)).all()
    created_ids: list[int] = []
    reactivated_ids: list[int] = []
    for notification_id, occurrence_id_value, fire_at, kind in affected:
        key = (occurrence_id_value, fire_at, kind)
        if key in existing:
            reactivated_ids.append(notification_id)
            # PostgreSQL upsert bypasses ORM state synchronization.  Refresh
            # only reactivated rows so callers do not observe stale CANCELLED
            # state; retry counters/error fields remain exactly as stored.
            await session.refresh(existing_objects[key])
        else:
            created_ids.append(notification_id)

    return ScheduleResult(
        created_notification_ids=tuple(created_ids),
        reactivated_notification_ids=tuple(reactivated_ids),
        existing_count=len(values) - len(affected),
    )


async def skip_occurrence(
    session: AsyncSession,
    *,
    user_id: int,
    occurrence_id: int,
    now_utc: dt.datetime,
) -> OccurrenceActionResult:
    """Skip one routine occurrence without altering its recurrence rule."""
    now_utc = _require_utc(now_utc, name="now_utc")
    await lock_user_context(session, user_id)
    await lock_occurrence_action(session, occurrence_id)
    row = (
        await session.execute(
            select(Occurrence, Entry)
            .join(Entry, Entry.id == Occurrence.entry_id)
            .where(Occurrence.id == occurrence_id, Occurrence.user_id == user_id)
            .with_for_update(of=Occurrence)
        )
    ).one_or_none()
    if row is None:
        raise DomainError("occurrence not found")
    occurrence, entry = row
    if entry.kind is not EntryKind.ROUTINE:
        raise DomainError("only a routine occurrence can be skipped")
    if occurrence.status is OccurrenceStatus.SKIPPED:
        return OccurrenceActionResult(occurrence_id=occurrence.id, changed=False)
    if occurrence.status is not OccurrenceStatus.PENDING:
        raise DomainError(f"occurrence in status {occurrence.status.value} cannot be skipped")

    occurrence.status = OccurrenceStatus.SKIPPED
    await session.execute(
        update(Notification)
        .where(
            Notification.occurrence_id == occurrence.id,
            Notification.status == NotificationStatus.PENDING,
        )
        .values(status=NotificationStatus.CANCELLED)
    )
    context = await session.scalar(
        select(ActiveContext).where(ActiveContext.user_id == user_id).with_for_update()
    )
    if context is not None and context.occurrence_id == occurrence.id:
        await session.delete(context)
    session.add(
        AuditLog(
            user_id=user_id,
            entity="occurrence",
            entity_id=occurrence.id,
            action=AuditAction.UPDATE,
            payload={
                "from": OccurrenceStatus.PENDING.value,
                "to": "skipped",
                "at": now_utc.isoformat(),
            },
        )
    )
    await session.flush()
    return OccurrenceActionResult(occurrence_id=occurrence.id, changed=True)


__all__ = [
    "MaterializeResult",
    "OccurrenceActionResult",
    "ReminderDefaults",
    "ScheduleResult",
    "materialize_occurrences",
    "resolve_wall_time",
    "schedule_notifications",
    "skip_occurrence",
]
