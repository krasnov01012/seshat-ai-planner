"""Безопасный рендер нормализованной записи в карточку подтверждения."""

from __future__ import annotations

import datetime as dt
import html
from collections.abc import Mapping
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

_KIND_RU = {"event": "событие", "task": "задачу", "routine": "рутину"}
_DAY_RU = {"MO": "пн", "TU": "вт", "WE": "ср", "TH": "чт", "FR": "пт", "SA": "сб", "SU": "вс"}


def confirmation_card(normalized: Mapping[str, Any]) -> str:
    kind = _scalar(normalized.get("kind"))
    title = html.escape(str(normalized.get("title") or "Без названия"))
    tz = str(normalized.get("tz") or "UTC")
    anchor = normalized.get("start_at_utc") or normalized.get("due_at_utc")
    local = _local_datetime(anchor, tz)
    duration = normalized.get("duration_min")
    recurrence = _format_rrule(normalized.get("rrule"))
    reminders = normalized.get("reminders_min_before") or []
    lines = [f"<b>Создать {_KIND_RU.get(kind, 'запись')}?</b>", "", f"<b>{title}</b>"]
    if local is not None:
        lines.extend(
            [
                f"Дата: {local:%d.%m.%Y}",
                f"Время: {local:%H:%M} ({html.escape(tz)})",
            ]
        )
    if duration:
        lines.append(f"Длительность: {_format_duration(int(duration))}")
    if recurrence:
        lines.append(f"Повторение: {recurrence}")
    lines.append(f"Напоминания: {_format_reminders(reminders)}")
    return "\n".join(lines)


def manual_fields_from_normalized(normalized: Mapping[str, Any]) -> dict[str, Any]:
    kind = _scalar(normalized.get("kind"))
    tz = str(normalized.get("tz") or "UTC")
    anchor = normalized.get("start_at_utc") or normalized.get("due_at_utc")
    local = _local_datetime(anchor, tz)
    if local is None:
        raise ValueError("normalized entry has no date/time")
    return {
        "kind": kind,
        "title": str(normalized.get("title") or ""),
        "local_date": local.date().isoformat(),
        "local_time": local.time().replace(tzinfo=None).isoformat(timespec="minutes"),
        "duration_min": normalized.get("duration_min"),
        "recurrence": _recurrence_from_rrule(normalized.get("rrule")),
        "reminders_min_before": list(normalized.get("reminders_min_before") or []),
    }


def _scalar(value: Any) -> str:
    return str(value.value if isinstance(value, Enum) else value)


def _local_datetime(value: Any, tz: str) -> dt.datetime | None:
    if value is None:
        return None
    moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(ZoneInfo(tz))


def _format_duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def _format_reminders(values: Any) -> str:
    minutes = [int(value) for value in values]
    if not minutes:
        return "по умолчанию"
    return ", ".join(
        "в момент" if value == 0 else f"за {_format_duration(value)}" for value in minutes
    )


def _format_rrule(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    parts = dict(part.split("=", 1) for part in text.split(";") if "=" in part)
    if parts.get("FREQ") == "DAILY":
        return "каждый день"
    if parts.get("FREQ") == "WEEKLY":
        days = [_DAY_RU.get(day, day) for day in parts.get("BYDAY", "").split(",") if day]
        return "по " + ", ".join(days) if days else "каждую неделю"
    if parts.get("FREQ") == "MONTHLY":
        return "каждый месяц"
    return html.escape(text)


def _recurrence_from_rrule(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    parts = dict(part.split("=", 1) for part in str(value).split(";") if "=" in part)
    reverse_days = {
        value: key
        for key, value in {
            "mon": "MO",
            "tue": "TU",
            "wed": "WE",
            "thu": "TH",
            "fri": "FR",
            "sat": "SA",
            "sun": "SU",
        }.items()
    }
    return {
        "freq": parts.get("FREQ", "DAILY").lower(),
        "byweekday": [
            reverse_days[day] for day in parts.get("BYDAY", "").split(",") if day in reverse_days
        ]
        or None,
        "interval": int(parts.get("INTERVAL", "1")),
    }
