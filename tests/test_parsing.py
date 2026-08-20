"""Разбор фразы: вывод типа, нормализация времени и отказ угадывать.

Тесты идут от реальных фраз тест-набора `tools/bench_nvidia_ru.py` и от
конкретных ошибок, которые модели допускали в замерах.
"""

from __future__ import annotations

import datetime as dt

import pytest

from seshat.db.enums import EntryKind
from seshat.domain.parsing import (
    Freq,
    Intent,
    NeedsClarification,
    NormalizedEntry,
    ParsedPlan,
    ParseError,
    Recurrence,
    derive_kind,
    normalize,
    to_utc,
)

TZ = "Europe/Moscow"
#: Понедельник 3 августа 2026, 10:00 по Москве.
NOW = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)


def plan(**kw: object) -> ParsedPlan:
    base: dict[str, object] = {"intent": Intent.CREATE, "confidence": 0.95, "title": "Тест"}
    return ParsedPlan(**{**base, **kw})  # type: ignore[arg-type]


def norm(p: ParsedPlan, *, tz: str = TZ) -> NormalizedEntry:
    return normalize(p, tz=tz, now_utc=NOW)


# --- вывод типа записи -------------------------------------------------------


def test_recurrence_makes_it_a_routine_even_if_model_said_event() -> None:
    """Главная находка замеров: 4 модели из 6 назвали рутину событием.

    Поле `kind` из ответа модели не используется вообще — тип выводится
    по наличию повторения.
    """
    p = plan(
        title="Тренировка",
        start=dt.datetime(2026, 8, 3, 19, 0),
        recurrence=Recurrence(freq=Freq.WEEKLY, byweekday=["mon", "wed", "fri"]),
    )
    assert derive_kind(p) is EntryKind.ROUTINE
    assert norm(p).rrule == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_deadline_without_start_is_a_task() -> None:
    p = plan(title="Закончить README", due=dt.datetime(2026, 8, 3, 20, 0))
    assert derive_kind(p) is EntryKind.TASK


def test_moment_without_recurrence_is_an_event() -> None:
    p = plan(title="Собеседование", start=dt.datetime(2026, 8, 4, 15, 0))
    assert derive_kind(p) is EntryKind.EVENT


# --- перевод времени ---------------------------------------------------------


def test_local_time_converted_to_utc() -> None:
    """Москва летом +3: 15:00 местного — это 12:00 UTC."""
    p = plan(title="Собеседование", start=dt.datetime(2026, 8, 4, 15, 0))
    assert norm(p).start_at_utc == dt.datetime(2026, 8, 4, 12, 0, tzinfo=dt.UTC)


def test_original_local_time_is_preserved() -> None:
    """Без исходного местного времени рутину нельзя пересчитать после переезда."""
    p = plan(
        title="Принять добавки",
        start=dt.datetime(2026, 8, 3, 8, 0),
        recurrence=Recurrence(freq=Freq.DAILY),
    )
    result = norm(p)
    assert result.local_time == dt.time(8, 0)
    assert result.tz == TZ


def test_same_wall_clock_differs_across_timezones() -> None:
    """Одно и то же «8:00» в разных поясах — разные моменты UTC.

    Летом: Москва UTC+3 круглый год, Амстердам UTC+2 (CEST) — разница час.
    """
    p = plan(title="Добавки", start=dt.datetime(2026, 8, 3, 8, 0))
    moscow = norm(p).start_at_utc
    amsterdam = normalize(p, tz="Europe/Amsterdam", now_utc=NOW).start_at_utc
    assert moscow == dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.UTC)
    assert amsterdam == dt.datetime(2026, 8, 3, 6, 0, tzinfo=dt.UTC)


def test_offset_between_zones_is_not_constant() -> None:
    """Разница между Москвой и Амстердамом меняется в течение года.

    Москва не переводит часы, Амстердам переводит: летом разница час,
    зимой — два. Поэтому пересчёт после переезда обязан идти через
    местное время и tzdata, а не через запомненное смещение.
    """
    summer = dt.datetime(2026, 8, 3, 8, 0)
    winter = dt.datetime(2026, 12, 3, 8, 0)

    summer_gap = to_utc(summer, "Europe/Amsterdam") - to_utc(summer, TZ)
    winter_gap = to_utc(winter, "Europe/Amsterdam") - to_utc(winter, TZ)

    assert summer_gap == dt.timedelta(hours=1)
    assert winter_gap == dt.timedelta(hours=2)


def test_dst_transition_is_deterministic() -> None:
    """В час, который случается дважды, результат не должен зависеть от версии tz."""
    ambiguous = dt.datetime(2026, 10, 25, 2, 30)
    first = to_utc(ambiguous, "Europe/Amsterdam")
    second = to_utc(ambiguous, "Europe/Amsterdam")
    assert first == second
    assert first.tzinfo is dt.UTC


# --- отказ угадывать ---------------------------------------------------------


def test_model_flagged_ambiguity_goes_to_clarification() -> None:
    p = plan(title="Подать документы", start=dt.datetime(2026, 8, 14, 9, 0))
    p.needs_clarification = True
    with pytest.raises(NeedsClarification):
        norm(p)


def test_low_confidence_goes_to_clarification() -> None:
    """«В следующую пятницу» модели разбирали тремя разными способами."""
    p = plan(title="Подать документы", start=dt.datetime(2026, 8, 7, 9, 0), confidence=0.55)
    with pytest.raises(NeedsClarification):
        norm(p)


def test_missing_time_goes_to_clarification() -> None:
    with pytest.raises(NeedsClarification):
        norm(plan(title="Что-то сделать"))


def test_missing_title_goes_to_clarification() -> None:
    with pytest.raises(NeedsClarification):
        norm(plan(title=None, start=dt.datetime(2026, 8, 4, 15, 0)))


def test_date_in_the_past_goes_to_clarification() -> None:
    """Типичная ошибка модели — прошлый год. Молча сохранять такое нельзя."""
    with pytest.raises(NeedsClarification, match="в прошлом"):
        norm(plan(title="Собеседование", start=dt.datetime(2025, 8, 4, 15, 0)))


def test_recent_past_is_allowed() -> None:
    """«Сделал сегодня в 8 утра» — законная запись, а не ошибка."""
    result = norm(plan(title="Зарядка", start=dt.datetime(2026, 8, 3, 8, 0)))
    assert result.start_at_utc == dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.UTC)


def test_routine_in_the_past_is_fine() -> None:
    """У правила повторения прошлое неважно — первый экземпляр найдёт материализатор."""
    p = plan(
        title="Добавки",
        start=dt.datetime(2026, 1, 1, 8, 0),
        recurrence=Recurrence(freq=Freq.DAILY),
    )
    assert norm(p).kind is EntryKind.ROUTINE


def test_non_create_intent_is_rejected() -> None:
    p = plan(title="Собеседование", start=dt.datetime(2026, 8, 4, 15, 0), intent=Intent.SNOOZE)
    with pytest.raises(ParseError, match="snooze"):
        norm(p)


# --- бизнес-правила ----------------------------------------------------------


def test_duration_over_a_day_is_rejected() -> None:
    p = plan(title="Проект", start=dt.datetime(2026, 8, 4, 10, 0), duration_min=2000)
    with pytest.raises(ParseError, match="больше суток"):
        norm(p)


def test_two_hours_duration_survives() -> None:
    """«Английский завтра в 12:00 на два часа» из тест-набора."""
    p = plan(title="Английский", start=dt.datetime(2026, 8, 4, 12, 0), duration_min=120)
    assert norm(p).duration_min == 120


def test_too_many_reminders_rejected() -> None:
    p = plan(
        title="Событие",
        start=dt.datetime(2026, 8, 4, 15, 0),
        reminders_min_before=[1, 2, 3, 4, 5, 6],
    )
    with pytest.raises(ParseError, match="напоминаний"):
        norm(p)


def test_reminders_deduplicated_and_sorted() -> None:
    """«Напомни за день и за час» — 1440 и 60, по убыванию и без дублей."""
    p = plan(
        title="Собеседование с А2",
        start=dt.datetime(2026, 8, 5, 15, 0),
        reminders_min_before=[60, 1440, 60],
    )
    assert norm(p).reminders_min_before == [1440, 60]


def test_negative_duration_rejected() -> None:
    p = plan(title="Событие", start=dt.datetime(2026, 8, 4, 15, 0), duration_min=-5)
    with pytest.raises(ParseError):
        norm(p)


# --- правила повторения ------------------------------------------------------


def test_daily_rrule() -> None:
    assert Recurrence(freq=Freq.DAILY).to_rrule() == "FREQ=DAILY"


def test_interval_rrule() -> None:
    assert Recurrence(freq=Freq.DAILY, interval=3).to_rrule() == "FREQ=DAILY;INTERVAL=3"


def test_weekday_names_normalised() -> None:
    r = Recurrence(freq=Freq.WEEKLY, byweekday=["Monday", "WED", "fri"])
    assert r.to_rrule() == "FREQ=WEEKLY;BYDAY=MO,WE,FR"


def test_unknown_weekday_rejected() -> None:
    with pytest.raises(ValueError, match="неизвестные дни"):
        Recurrence(freq=Freq.WEEKLY, byweekday=["mon", "xyz"])


def test_title_whitespace_collapsed() -> None:
    p = plan(title="  Собеседование   с   А2  ", start=dt.datetime(2026, 8, 4, 15, 0))
    assert norm(p).title == "Собеседование с А2"


def test_timezone_from_model_is_rejected_not_silently_discarded() -> None:
    """Снятие offset молча сдвинуло бы фактический момент события."""
    with pytest.raises(ValueError, match="без UTC-смещения"):
        plan(
            title="Созвон",
            start=dt.datetime(2026, 8, 4, 15, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))),
        )


def test_zero_recurrence_interval_is_rejected() -> None:
    with pytest.raises(ValueError):
        Recurrence(freq=Freq.DAILY, interval=0)


def test_confidence_outside_probability_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        plan(title="Созвон", start=dt.datetime(2026, 8, 4, 15, 0), confidence=1.5)


def test_unexpected_model_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        ParsedPlan.model_validate(
            {
                "intent": "create",
                "title": "Созвон",
                "start": "2026-08-04T15:00:00",
                "kind": "event",
            }
        )


def test_normalize_rejects_naive_current_time() -> None:
    parsed = plan(title="Созвон", start=dt.datetime(2026, 8, 4, 15, 0))
    with pytest.raises(ValueError, match="tz-aware"):
        normalize(parsed, tz="Europe/Moscow", now_utc=dt.datetime(2026, 8, 3, 7, 0))
