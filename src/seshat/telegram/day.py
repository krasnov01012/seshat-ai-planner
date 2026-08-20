"""Telegram-адаптер read model «Мой день»."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from seshat.db.base import session_scope
from seshat.domain import get_or_create_user
from seshat.domain.day import MyDay, MyDayItem, MyDayNightItem, get_my_day
from seshat.telegram.contracts import TelegramDependencies
from seshat.telegram.router import OwnerOnly

_TELEGRAM_SAFE_CHARS = 3_800
_DISPLAY_TITLE_CHARS = 180


def build_day_router(owner_id: int, dependencies: TelegramDependencies) -> Router:
    router = Router(name="my-day")
    router.message.filter(OwnerOnly(owner_id))

    @router.message(Command("day"))
    @router.message(F.text.casefold() == "мой день")
    async def show_day(message: Message) -> None:
        async with session_scope(dependencies.session_factory) as session:
            user = await get_or_create_user(
                session,
                owner_id,
                default_tz=dependencies.default_tz,
            )
            day = await get_my_day(session, user.id, now_utc=dependencies.clock())
        await message.answer(my_day_text(day))

    return router


def my_day_text(day: MyDay) -> str:
    lines: list[str] = []
    if day.night:
        _append_section(
            lines,
            "<b>Пришло ночью</b>",
            [_night_line(item) for item in day.night],
        )
    if day.missed:
        _append_section(
            lines,
            "<b>Пропущено</b>",
            [_item_line(item, "⚠️") for item in day.missed],
        )
    if day.items:
        _append_section(
            lines,
            "<b>Сегодня</b>",
            [_item_line(item, "•") for item in day.items],
        )
    if not lines:
        return "На сегодня записей нет."
    return "\n".join(lines)


def _append_section(lines: list[str], heading: str, rows: list[str]) -> None:
    prefix = ["", heading] if lines else [heading]
    if len("\n".join([*lines, *prefix])) > _TELEGRAM_SAFE_CHARS:
        return
    lines.extend(prefix)
    appended = 0
    for index, row in enumerate(rows):
        remaining = len(rows) - index - 1
        reserve = f"… и ещё {remaining}" if remaining else ""
        candidate = [*lines, row]
        if reserve:
            candidate.append(reserve)
        if len("\n".join(candidate)) > _TELEGRAM_SAFE_CHARS:
            break
        lines.append(row)
        appended += 1
    remaining = len(rows) - appended
    if remaining:
        summary = f"… и ещё {remaining}"
        while appended and len("\n".join([*lines, summary])) > _TELEGRAM_SAFE_CHARS:
            lines.pop()
            appended -= 1
            remaining += 1
            summary = f"… и ещё {remaining}"
        if len("\n".join([*lines, summary])) <= _TELEGRAM_SAFE_CHARS:
            lines.append(summary)


def _item_line(item: MyDayItem, marker: str) -> str:
    when = item.planned_at_local.strftime("%H:%M")
    return f"{marker} {when} — {_display_title(item.title)}"


def _night_line(item: MyDayNightItem) -> str:
    when = item.sent_at_local.strftime("%H:%M")
    return f"🌙 {when} — {_display_title(item.title)}"


def _display_title(title: str) -> str:
    shortened = title if len(title) <= _DISPLAY_TITLE_CHARS else f"{title[:179]}…"
    return html.escape(shortened)


__all__ = ["build_day_router", "my_day_text"]
