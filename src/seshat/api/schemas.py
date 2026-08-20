"""Схемы API.

Отделены от моделей БД намеренно: контракт наружу не должен меняться каждый раз,
когда меняется внутреннее хранение. Это же даёт осмысленный OpenAPI.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seshat.db.enums import EntryKind, EntryStatus, Importance, Persistence
from seshat.domain.entries import ManualEntryInput
from seshat.domain.parsing import NormalizedEntry
from seshat.domain.timezones import TimezoneReviewDecision


class HealthOut(BaseModel):
    status: str = "ok"
    version: str
    env: str


class ReadinessOut(BaseModel):
    status: str
    database: str
    #: Применённая ревизия Alembic. Расхождение с кодом видно сразу.
    migration: str | None = None


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: int
    tz: str
    quiet_from: dt.time
    quiet_to: dt.time
    digest_enabled: bool
    digest_time: dt.time
    review_time: dt.time
    week_start: int
    default_snooze_min: int
    confirm_before_save: bool


class QuietHoursIn(BaseModel):
    quiet_from: dt.time = Field(examples=["23:00"])
    quiet_to: dt.time = Field(examples=["08:00"])

    @field_validator("quiet_from", "quiet_to")
    @classmethod
    def _local_wall_time_only(cls, value: dt.time) -> dt.time:
        if value.tzinfo is not None:
            raise ValueError("тихие часы задаются местным временем без UTC-смещения")
        return value


class TimezoneIn(BaseModel):
    tz: str = Field(examples=["Europe/Amsterdam"], description="Идентификатор IANA")


class TimezoneConfirmIn(TimezoneIn):
    expected_tz_from: str = Field(
        examples=["Europe/Moscow"], description="Таймзона из подтверждаемой карточки"
    )
    confirmation_id: uuid.UUID


class TimezoneChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tz_from: str
    tz_to: str
    changed_at_utc: dt.datetime
    #: Разобрал ли владелец будущие события после переезда.
    entries_reviewed: bool


class TimezonePreviewOut(BaseModel):
    confirmation_id: uuid.UUID
    tz_from: str
    tz_to: str
    now_from: dt.datetime
    now_to: dt.datetime
    routine_count: int
    review_count: int


class TimezoneReviewItemOut(BaseModel):
    entry_id: int
    kind: EntryKind
    title: str
    moment_field: str
    moment_at_utc: dt.datetime
    keep_absolute_local: dt.datetime
    keep_local_at_utc: dt.datetime
    keep_local_local: dt.datetime


class TimezoneConfirmOut(BaseModel):
    change: TimezoneChangeOut
    applied: bool
    routines_rebased: int
    review_total: int
    review_remaining: int
    next_review: TimezoneReviewItemOut | None = None


class TimezoneReviewIn(BaseModel):
    decision: TimezoneReviewDecision


class TimezoneReviewOut(BaseModel):
    change_id: int
    entry_id: int
    decision: TimezoneReviewDecision
    applied: bool
    review_remaining: int
    next_review: TimezoneReviewItemOut | None = None


class ErrorOut(BaseModel):
    detail: str


class ManualEntryIn(ManualEntryInput):
    """Ручная форма в локальном времени; повторяет domain-контракт без логики."""


class EntryDraftSchema(NormalizedEntry):
    """Нормализованная карточка, которую клиент затем подтверждает без изменений."""


class EntryPreviewOut(BaseModel):
    confirmation_id: uuid.UUID
    manual: ManualEntryIn
    draft: EntryDraftSchema


class EntryConfirmIn(BaseModel):
    confirmation_id: uuid.UUID
    manual: ManualEntryIn | None = None
    text: str | None = Field(default=None, min_length=1, max_length=4096)
    draft: EntryDraftSchema

    @model_validator(mode="after")
    def _exactly_one_source(self) -> EntryConfirmIn:
        if (self.manual is None) == (self.text is None):
            raise ValueError("нужно передать ровно один источник: manual или text")
        return self


class TextEntryIn(BaseModel):
    text: str = Field(min_length=1, max_length=4096, examples=["Завтра в 15:00 собеседование"])


class TextReadyOut(BaseModel):
    status: Literal["ready"] = "ready"
    confirmation_id: uuid.UUID
    text: str
    draft: EntryDraftSchema


class TextClarificationOut(BaseModel):
    status: Literal["clarification"] = "clarification"
    prompt: str


class TextManualFallbackOut(BaseModel):
    status: Literal["manual_fallback"] = "manual_fallback"
    prompt: str


type TextPreparationOut = TextReadyOut | TextClarificationOut | TextManualFallbackOut


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    kind: EntryKind
    title: str
    notes: str | None
    start_at_utc: dt.datetime | None
    due_at_utc: dt.datetime | None
    duration_min: int | None
    rrule: str | None
    tz: str
    local_time: dt.time | None
    importance: Importance
    persistence: Persistence
    reminders_min_before: list[int]
    status: EntryStatus
    created_at: dt.datetime
    updated_at: dt.datetime
    deleted_at: dt.datetime | None


class EntryCreateOut(BaseModel):
    created: bool
    entry: EntryOut
