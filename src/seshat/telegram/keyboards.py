"""Telegram-клавиатуры с намеренно короткими callback payload."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Мой день")],
            [KeyboardButton(text="Добавить"), KeyboardButton(text="Настройки")],
        ],
        resize_keyboard=True,
    )


def kind_keyboard(prefix: str = "manual") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Событие", callback_data=f"{prefix}:kind:event"),
                InlineKeyboardButton(text="Задача", callback_data=f"{prefix}:kind:task"),
                InlineKeyboardButton(text="Рутина", callback_data=f"{prefix}:kind:routine"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data="common:cancel")],
        ]
    )


def date_keyboard(prefix: str = "manual") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=f"{prefix}:date:today"),
                InlineKeyboardButton(text="Завтра", callback_data=f"{prefix}:date:tomorrow"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data="common:cancel")],
        ]
    )


def time_keyboard(prefix: str = "manual") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="09:00", callback_data=f"{prefix}:time:09:00"),
                InlineKeyboardButton(text="12:00", callback_data=f"{prefix}:time:12:00"),
                InlineKeyboardButton(text="18:00", callback_data=f"{prefix}:time:18:00"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data="common:cancel")],
        ]
    )


def duration_keyboard(prefix: str = "manual") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Без длительности", callback_data=f"{prefix}:dur:none"),
            ],
            [
                InlineKeyboardButton(text="30 мин", callback_data=f"{prefix}:dur:30"),
                InlineKeyboardButton(text="1 час", callback_data=f"{prefix}:dur:60"),
                InlineKeyboardButton(text="1,5 часа", callback_data=f"{prefix}:dur:90"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data="common:cancel")],
        ]
    )


def recurrence_keyboard(prefix: str = "manual") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Каждый день", callback_data=f"{prefix}:rec:daily"),
                InlineKeyboardButton(text="По будням", callback_data=f"{prefix}:rec:weekdays"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data="common:cancel")],
        ]
    )


def confirmation_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Создать", callback_data=f"confirm:create:{nonce}"),
                InlineKeyboardButton(text="Изменить", callback_data=f"confirm:edit:{nonce}"),
                InlineKeyboardButton(text="Отменить", callback_data=f"confirm:cancel:{nonce}"),
            ]
        ]
    )


def edit_keyboard(nonce: str, *, routine: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="Тип", callback_data=f"edit:kind:{nonce}"),
            InlineKeyboardButton(text="Дата", callback_data=f"edit:date:{nonce}"),
            InlineKeyboardButton(text="Время", callback_data=f"edit:time:{nonce}"),
        ],
        [
            InlineKeyboardButton(text="Длительность", callback_data=f"edit:duration:{nonce}"),
            InlineKeyboardButton(text="Название", callback_data=f"edit:title:{nonce}"),
        ],
        [InlineKeyboardButton(text="Напоминания", callback_data=f"edit:reminders:{nonce}")],
    ]
    if routine:
        rows.insert(
            2,
            [InlineKeyboardButton(text="Повторение", callback_data=f"edit:recurrence:{nonce}")],
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data=f"edit:back:{nonce}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fallback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Заполнить по шагам", callback_data="manual:start")],
            [InlineKeyboardButton(text="Отменить", callback_data="common:cancel")],
        ]
    )
