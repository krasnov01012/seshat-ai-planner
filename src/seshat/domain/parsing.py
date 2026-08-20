"""Разбор фразы в структуру записи.

Здесь нет обращений к модели — только схема её ответа, нормализация и правила.
Модель извлекает поля, решения принимает этот код: обоснование в docs/AI_MODELS.md.

Два правила, ради которых модуль и существует:

* **тип записи выводится кодом.** В замерах все модели верно извлекли повторение
  «пн, ср, пт 19:00», но четыре из шести назвали это событием вместо рутины;
* **неоднозначность не угадывается.** На «в следующую пятницу» модели выдали
  7 августа, 8 августа (субботу) и `null` — такое обязано уходить в переспрос.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seshat.db.enums import EntryKind

#: Ниже этого значения бот переспрашивает, а не показывает карточку.
CONFIDENCE_THRESHOLD = 0.7

#: Больше суток — почти наверняка ошибка разбора, а не намерение пользователя.
MAX_DURATION_MIN = 24 * 60

#: Больше пяти напоминаний на одну запись превращают бота в источник раздражения.
MAX_REMINDERS = 5

#: Насколько глубоко в прошлое допускается время начала. Пользователь может
#: записывать уже случившееся («сделал в 8 утра»), но не прошлогоднее.
PAST_TOLERANCE = dt.timedelta(hours=12)

_WEEKDAYS = {
    "mon": "MO",
    "tue": "TU",
    "wed": "WE",
    "thu": "TH",
    "fri": "FR",
    "sat": "SA",
    "sun": "SU",
}


class Intent(StrEnum):
    CREATE = "create"
    RESCHEDULE = "reschedule"
    SNOOZE = "snooze"
    COMPLETE = "complete"
    SKIP = "skip"
    UNKNOWN = "unknown"


class Freq(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class ParseError(Exception):
    """Разобранное не проходит бизнес-правила. Уходит в переспрос, не в БД."""


class NeedsClarification(Exception):
    """Фраза понята неоднозначно. Бот обязан спросить, а не решать за пользователя."""


class Recurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    freq: Freq
    byweekday: list[str] | None = None
    interval: int = Field(default=1, ge=1)

    @field_validator("byweekday")
    @classmethod
    def _known_days(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        days = [d[:3].lower() for d in v]
        unknown = [d for d in days if d not in _WEEKDAYS]
        if unknown:
            raise ValueError(f"неизвестные дни недели: {unknown}")
        return days

    def to_rrule(self) -> str:
        """RRULE по RFC 5545. Разворачивать его будет материализатор этапа 3."""
        parts = [f"FREQ={self.freq.value.upper()}"]
        if self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.byweekday:
            parts.append("BYDAY=" + ",".join(_WEEKDAYS[d] for d in self.byweekday))
        return ";".join(parts)


class ParsedPlan(BaseModel):
    """Ответ модели. Все моменты — местное время пользователя, без таймзоны.

    Таймзону подставляет код: просить модель считать смещения — это лишний
    источник ошибок там, где обычная арифметика точна.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent = Intent.UNKNOWN
    title: str | None = None
    start: dt.datetime | None = None
    due: dt.datetime | None = None
    duration_min: int | None = None
    recurrence: Recurrence | None = None
    reminders_min_before: list[int] = Field(default_factory=list)
    snooze_min: int | None = None
    target_ref: str | None = None
    needs_clarification: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)

    @field_validator("start", "due")
    @classmethod
    def _must_be_naive(cls, v: dt.datetime | None) -> dt.datetime | None:
        if v is not None and v.tzinfo is not None and v.utcoffset() is not None:
            # Снять offset молча нельзя: 15:00Z и 15:00 Europe/Moscow —
            # разные моменты. Таймзону задаёт код, поэтому такой ответ
            # нарушает контракт и должен уйти в переспрос.
            raise ValueError("дата модели должна быть локальной и без UTC-смещения")
        return v

    @field_validator("title")
    @classmethod
    def _clean_title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = " ".join(v.split())
        return cleaned[:500] or None


class NormalizedEntry(BaseModel):
    """Готовое к сохранению. Тип уже выведен кодом, времена уже в UTC."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EntryKind
    title: str = Field(min_length=1, max_length=500)
    start_at_utc: dt.datetime | None = None
    due_at_utc: dt.datetime | None = None
    duration_min: int | None = Field(default=None, gt=0, le=MAX_DURATION_MIN)
    rrule: str | None = None
    tz: str
    local_time: dt.time | None = None
    reminders_min_before: list[int] = Field(default_factory=list, max_length=MAX_REMINDERS)

    @field_validator("start_at_utc", "due_at_utc")
    @classmethod
    def _must_be_aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("нормализованная дата должна содержать UTC-смещение")
        if value is not None and value.utcoffset() != dt.timedelta(0):
            raise ValueError("нормализованная дата должна быть в UTC")
        return value

    @field_validator("tz")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"неизвестная таймзона IANA: {value!r}") from exc
        return value

    @field_validator("reminders_min_before")
    @classmethod
    def _normalised_reminders(cls, value: list[int]) -> list[int]:
        if any(item < 0 for item in value):
            raise ValueError("смещение напоминания не может быть отрицательным")
        expected = sorted(set(value), reverse=True)
        if value != expected:
            raise ValueError("напоминания должны быть уникальны и отсортированы по убыванию")
        return value

    @model_validator(mode="after")
    def _kind_invariants(self) -> NormalizedEntry:
        if self.kind is EntryKind.EVENT and self.start_at_utc is None:
            raise ValueError("для события требуется время начала")
        if self.kind is EntryKind.ROUTINE:
            if self.rrule is None:
                raise ValueError("для рутины требуется правило повторения")
            if self.start_at_utc is None and self.due_at_utc is None:
                raise ValueError("для рутины требуется время выполнения")
        elif self.rrule is not None:
            raise ValueError("правило повторения допустимо только для рутины")
        return self


def derive_kind(plan: ParsedPlan) -> EntryKind:
    """Тип записи определяет код, а не модель.

    Поле `kind` из ответа модели игнорируется сознательно: извлечение слотов
    у моделей надёжное, классификация — нет.
    """
    if plan.recurrence is not None:
        return EntryKind.ROUTINE
    if plan.due is not None and plan.start is None:
        return EntryKind.TASK
    if plan.start is not None:
        return EntryKind.EVENT
    return EntryKind.TASK


def to_utc(moment: dt.datetime, tz: str) -> dt.datetime:
    """Местное время → UTC.

    `fold=0` фиксирует выбор в час, который при переходе на зимнее время
    случается дважды: без этого результат зависит от версии библиотеки.
    """
    return moment.replace(tzinfo=ZoneInfo(tz), fold=0).astimezone(dt.UTC)


def normalize(
    plan: ParsedPlan,
    *,
    tz: str,
    now_utc: dt.datetime,
    allow_undated_task: bool = False,
) -> NormalizedEntry:
    """Проверяет бизнес-правила и переводит в вид, пригодный для БД.

    Бросает `NeedsClarification`, если фразу нельзя понять однозначно,
    и `ParseError`, если разобранное нарушает правила.
    """
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc должен быть tz-aware")
    if plan.needs_clarification:
        raise NeedsClarification("модель отметила фразу как неоднозначную")
    if plan.confidence < CONFIDENCE_THRESHOLD:
        raise NeedsClarification(
            f"низкая уверенность разбора: {plan.confidence:.2f} < {CONFIDENCE_THRESHOLD}"
        )
    if plan.intent is not Intent.CREATE:
        raise ParseError(f"это не создание записи, а {plan.intent.value}")
    if not plan.title:
        raise NeedsClarification("не удалось понять, что именно нужно записать")

    kind = derive_kind(plan)

    if kind is EntryKind.ROUTINE and plan.start is None and plan.due is None:
        raise NeedsClarification("для рутины не понято время выполнения")
    if (
        kind is not EntryKind.ROUTINE
        and plan.start is None
        and plan.due is None
        and not (allow_undated_task and kind is EntryKind.TASK)
    ):
        raise NeedsClarification("не понята дата или время")

    if plan.duration_min is not None:
        if plan.duration_min <= 0:
            raise ParseError("длительность должна быть положительной")
        if plan.duration_min > MAX_DURATION_MIN:
            raise ParseError(
                f"длительность {plan.duration_min} мин больше суток — вероятна ошибка разбора"
            )

    reminders = sorted({r for r in plan.reminders_min_before if r >= 0}, reverse=True)
    if len(reminders) > MAX_REMINDERS:
        raise ParseError(f"больше {MAX_REMINDERS} напоминаний на одну запись")

    start_utc = to_utc(plan.start, tz) if plan.start else None
    due_utc = to_utc(plan.due, tz) if plan.due else None

    # У рутины «прошлое» бессмысленно: правило повторения смотрит вперёд,
    # а первый экземпляр подберёт материализатор.
    if kind is not EntryKind.ROUTINE:
        anchor = start_utc or due_utc
        if anchor is not None and anchor < now_utc - PAST_TOLERANCE:
            raise NeedsClarification(
                "получилась дата в прошлом — уточни, какой год или день имелся в виду"
            )

    local = plan.start or plan.due
    return NormalizedEntry(
        kind=kind,
        title=plan.title,
        start_at_utc=start_utc,
        due_at_utc=due_utc,
        duration_min=plan.duration_min,
        rrule=plan.recurrence.to_rrule() if plan.recurrence else None,
        tz=tz,
        local_time=local.time() if local else None,
        reminders_min_before=reminders,
    )
