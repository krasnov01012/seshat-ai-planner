"""Живая приёмка разбора этапа 2 через production-контракт NIM.

Скрипт намеренно выполняет запросы последовательно: это одновременно проверяет
контракт ``NimClient`` и не повторяет нагрузочный сценарий, который уже дал 429.
Секреты и сырые ответы провайдера не печатаются.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any

from seshat.config import load_settings
from seshat.db.enums import EntryKind
from seshat.domain.nim import NimClient
from seshat.domain.parsing import NeedsClarification, normalize

NOW_UTC = dt.datetime(2026, 8, 3, 9, 0, tzinfo=dt.UTC)
TZ = "Europe/Moscow"


@dataclass(frozen=True, slots=True)
class Case:
    name: str
    text: str
    expected: dict[str, Any]


CASES = (
    Case(
        "event_tomorrow",
        "Завтра в 15:00 собеседование",
        {"kind": EntryKind.EVENT, "start_at_utc": "2026-08-04T12:00:00+00:00"},
    ),
    Case(
        "routine_daily",
        "Каждый день в 8:00 принимать добавки",
        {"kind": EntryKind.ROUTINE, "rrule": "FREQ=DAILY", "local_time": "08:00:00"},
    ),
    Case(
        "routine_weekdays",
        "По будням в 9:30 разбирать почту",
        {
            "kind": EntryKind.ROUTINE,
            "rrule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
            "local_time": "09:30:00",
        },
    ),
    Case(
        "routine_mwf",
        "По понедельникам, средам и пятницам тренировка в 19:00",
        {
            "kind": EntryKind.ROUTINE,
            "rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
            "local_time": "19:00:00",
        },
    ),
    Case(
        "task_deadline",
        "Сегодня до 20:00 закончить README",
        {"kind": EntryKind.TASK, "due_at_utc": "2026-08-03T17:00:00+00:00"},
    ),
    Case(
        "event_duration",
        "Английский завтра в 12:00 на два часа",
        {
            "kind": EntryKind.EVENT,
            "start_at_utc": "2026-08-04T09:00:00+00:00",
            "duration_min": 120,
        },
    ),
    Case(
        "event_reminders",
        "Через два дня в 15:00 собеседование. Напомни за день и за час",
        {
            "kind": EntryKind.EVENT,
            "start_at_utc": "2026-08-05T12:00:00+00:00",
            "reminders_min_before": [1440, 60],
        },
    ),
    Case(
        "event_exact_date",
        "15 августа в 09:00 поезд в Санкт-Петербург",
        {"kind": EntryKind.EVENT, "start_at_utc": "2026-08-15T06:00:00+00:00"},
    ),
    Case(
        "task_exact_date",
        "До 1 сентября 18:00 оплатить интернет",
        {"kind": EntryKind.TASK, "due_at_utc": "2026-09-01T15:00:00+00:00"},
    ),
    Case(
        "routine_weekly",
        "Каждое воскресенье в 11:00 планировать неделю",
        {
            "kind": EntryKind.ROUTINE,
            "rrule": "FREQ=WEEKLY;BYDAY=SU",
            "local_time": "11:00:00",
        },
    ),
)


def _actual(entry: object, field: str) -> object:
    value = getattr(entry, field)
    if isinstance(value, (dt.datetime, dt.time)):
        return value.isoformat()
    return value


async def main() -> None:
    settings = load_settings()
    failures: list[str] = []

    async with NimClient(settings) as client:
        for case in CASES:
            result = await client.parse(case.text, tz=TZ, now_utc=NOW_UTC)
            entry = normalize(result.plan, tz=TZ, now_utc=NOW_UTC)
            errors = [
                f"{field}: ожидалось {expected!r}, получено {_actual(entry, field)!r}"
                for field, expected in case.expected.items()
                if _actual(entry, field) != expected
            ]
            if errors:
                failures.append(f"{case.name}: " + "; ".join(errors))
                print(f"FAIL {case.name}")
            else:
                print(f"OK   {case.name} ({result.model}, {result.latency_ms} ms)")

        ambiguous = await client.parse(
            "В следующую пятницу утром подать документы", tz=TZ, now_utc=NOW_UTC
        )
        try:
            normalize(ambiguous.plan, tz=TZ, now_utc=NOW_UTC)
        except NeedsClarification:
            print("OK   ambiguous_requires_clarification")
        else:
            failures.append("ambiguous_requires_clarification: модель/домен молча выбрали дату")
            print("FAIL ambiguous_requires_clarification")

    if failures:
        raise SystemExit("\n".join(failures))
    print(f"\nStage 2 NIM acceptance: {len(CASES)}/10 + ambiguity OK")


if __name__ == "__main__":
    asyncio.run(main())
