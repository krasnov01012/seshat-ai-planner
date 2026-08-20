"""Подготовка и подтверждённое создание записей.

Оба адаптера получают один и тот же неизменяемый ``EntryDraft``. Preview не
пишет в БД, а подтверждение атомарно создаёт ``Entry`` и ``AuditLog``.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.db.enums import AuditAction, EntryKind
from seshat.db.models import AuditLog, Entry
from seshat.domain.parsing import (
    Freq,
    Intent,
    NeedsClarification,
    NormalizedEntry,
    ParsedPlan,
    ParseError,
    Recurrence,
    normalize,
)
from seshat.domain.users import DomainError, validate_tz

EntryDraft = NormalizedEntry


class EntryValidationError(DomainError):
    """Ручной ввод не образует допустимую запись."""


class ConfirmationConflictError(DomainError):
    """Один confirmation_id повторно использован с другими данными."""


class ManualEntryInput(BaseModel):
    """Поля ручной формы в местном времени пользователя, без обращения к AI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind
    title: str = Field(min_length=1, max_length=500)
    start: dt.datetime | None = None
    due: dt.datetime | None = None
    duration_min: int | None = None
    recurrence: Recurrence | None = None
    reminders_min_before: list[int] = Field(default_factory=list)

    @field_validator("start", "due")
    @classmethod
    def _must_be_naive(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("ручная дата должна быть местной и без UTC-смещения")
        return value

    @model_validator(mode="after")
    def _fields_match_kind(self) -> ManualEntryInput:
        if self.kind is EntryKind.EVENT:
            if self.start is None:
                raise ValueError("для события требуется время начала")
            if self.due is not None or self.recurrence is not None:
                raise ValueError("событие принимает start, но не due или recurrence")
        elif self.kind is EntryKind.TASK:
            if self.start is not None or self.recurrence is not None:
                raise ValueError("задача принимает необязательный due, но не start или recurrence")
        elif self.kind is EntryKind.ROUTINE:
            if self.start is None or self.recurrence is None:
                raise ValueError("для рутины требуются start и recurrence")
            if self.due is not None:
                raise ValueError("рутина не принимает due")
        return self


class EntryPreview(BaseModel):
    """Карточка и одноразовый идентификатор её подтверждения."""

    model_config = ConfigDict(frozen=True)

    confirmation_id: uuid.UUID
    draft: EntryDraft


class CreateEntryResult(BaseModel):
    """Результат подтверждения; ``created=False`` означает безопасный replay."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    entry: Entry
    created: bool


def prepare_manual_entry(manual: ManualEntryInput, *, tz: str, now_utc: dt.datetime) -> EntryDraft:
    """Проводит ручную форму через ту же нормализацию, что и результат AI."""
    validate_tz(tz)
    plan = ParsedPlan(
        intent=Intent.CREATE,
        confidence=1.0,
        title=manual.title,
        start=manual.start,
        due=manual.due,
        duration_min=manual.duration_min,
        recurrence=manual.recurrence,
        reminders_min_before=manual.reminders_min_before,
    )
    try:
        draft = normalize(
            plan,
            tz=tz,
            now_utc=now_utc,
            allow_undated_task=manual.kind is EntryKind.TASK,
        )
    except (NeedsClarification, ParseError) as exc:
        raise EntryValidationError(str(exc)) from exc
    if draft.kind is not manual.kind:
        raise EntryValidationError(
            f"поля соответствуют типу {draft.kind.value}, а выбран {manual.kind.value}"
        )
    return draft


def preview_entry(draft: EntryDraft, *, confirmation_id: uuid.UUID | None = None) -> EntryPreview:
    """Создаёт чистый preview; состояние появится только после подтверждения."""
    return EntryPreview(confirmation_id=confirmation_id or uuid.uuid4(), draft=draft)


def preview_manual_entry(
    manual: ManualEntryInput,
    *,
    tz: str,
    now_utc: dt.datetime,
    confirmation_id: uuid.UUID | None = None,
) -> EntryPreview:
    return preview_entry(
        prepare_manual_entry(manual, tz=tz, now_utc=now_utc),
        confirmation_id=confirmation_id,
    )


def _draft_payload(draft: EntryDraft) -> dict[str, object]:
    return draft.model_dump(mode="json")


async def create_entry(
    session: AsyncSession,
    user_id: int,
    draft: EntryDraft,
    *,
    confirmation_id: uuid.UUID,
) -> CreateEntryResult:
    """Атомарно создаёт Entry+AuditLog и безопасно повторяет двойное нажатие.

    Транзакционный advisory lock действует между отдельными процессами API и
    бота. ``AuditLog`` хранит устойчивое соответствие confirmation_id → Entry,
    поэтому миграция схемы только ради идемпотентности не нужна.
    """
    confirmation = str(confirmation_id)
    lock_key = f"seshat:create-entry:{user_id}:{confirmation}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )

    existing_audit = (
        await session.execute(
            select(AuditLog)
            .where(
                AuditLog.user_id == user_id,
                AuditLog.entity == "entry",
                AuditLog.action == AuditAction.CREATE,
                AuditLog.payload["confirmation_id"].as_string() == confirmation,
            )
            .order_by(AuditLog.id)
            .limit(1)
        )
    ).scalar_one_or_none()

    payload = _draft_payload(draft)
    if existing_audit is not None:
        if (existing_audit.payload or {}).get("draft") != payload:
            raise ConfirmationConflictError("confirmation_id уже использован для другой карточки")
        if existing_audit.entity_id is None:
            raise DomainError("журнал подтверждения не содержит идентификатор записи")
        existing_entry = await session.get(Entry, existing_audit.entity_id)
        if existing_entry is None:
            raise DomainError("подтверждённая запись не найдена")
        return CreateEntryResult(entry=existing_entry, created=False)

    entry = Entry(
        user_id=user_id,
        kind=draft.kind,
        title=draft.title,
        start_at_utc=draft.start_at_utc,
        due_at_utc=draft.due_at_utc,
        duration_min=draft.duration_min,
        rrule=draft.rrule,
        tz=draft.tz,
        local_time=draft.local_time,
        reminders_min_before=list(draft.reminders_min_before),
    )
    session.add(entry)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user_id,
            entity="entry",
            entity_id=entry.id,
            action=AuditAction.CREATE,
            payload={"confirmation_id": confirmation, "draft": payload},
        )
    )
    await session.flush()
    return CreateEntryResult(entry=entry, created=True)


__all__ = [
    "ConfirmationConflictError",
    "CreateEntryResult",
    "EntryDraft",
    "EntryPreview",
    "EntryValidationError",
    "Freq",
    "ManualEntryInput",
    "Recurrence",
    "create_entry",
    "prepare_manual_entry",
    "preview_entry",
    "preview_manual_entry",
]
