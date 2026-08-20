"""Aiogram-адаптер доставки сохранённых уведомлений."""

from __future__ import annotations

import html

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from seshat.db.enums import EntryKind, NotificationKind
from seshat.domain.delivery import (
    DeliveryCommand,
    DeliveryReceipt,
    PermanentDeliveryError,
    TransientDeliveryError,
)
from seshat.domain.digests import MorningDigestCommand
from seshat.telegram.day import my_day_text


class AiogramNotificationTransport:
    """Переводит доменную команду в Telegram API без бизнес-решений."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, command: DeliveryCommand) -> DeliveryReceipt:
        try:
            message = await self._bot.send_message(
                chat_id=command.telegram_id,
                text=notification_text(command),
                disable_notification=command.silent,
                reply_markup=notification_keyboard(command),
            )
        except TelegramRetryAfter as exc:
            raise TransientDeliveryError(
                "telegram_rate_limit", retry_after_s=int(exc.retry_after)
            ) from exc
        except (TelegramNetworkError, TelegramServerError) as exc:
            raise TransientDeliveryError("telegram_unavailable") from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentDeliveryError("telegram_rejected") from exc
        except TelegramAPIError as exc:
            raise TransientDeliveryError("telegram_api_error") from exc
        return DeliveryReceipt(message_id=message.message_id)

    async def send_digest(self, command: MorningDigestCommand) -> DeliveryReceipt:
        try:
            message = await self._bot.send_message(
                chat_id=command.telegram_id,
                text=my_day_text(command.day),
                disable_notification=True,
            )
        except TelegramRetryAfter as exc:
            raise TransientDeliveryError(
                "telegram_rate_limit", retry_after_s=int(exc.retry_after)
            ) from exc
        except (TelegramNetworkError, TelegramServerError) as exc:
            raise TransientDeliveryError("telegram_unavailable") from exc
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            raise PermanentDeliveryError("telegram_rejected") from exc
        except TelegramAPIError as exc:
            raise TransientDeliveryError("telegram_api_error") from exc
        return DeliveryReceipt(message_id=message.message_id)


def notification_text(command: DeliveryCommand) -> str:
    heading = {
        NotificationKind.PRE: "Скоро",
        NotificationKind.MAIN: "Сейчас",
        NotificationKind.REPEAT: "Напоминаю",
    }[command.notification_kind]
    lines = [f"<b>{heading}</b>", html.escape(command.title)]
    if command.entry_kind is EntryKind.ROUTINE:
        lines.append(f"Пропустить только этот раз: <code>/skip {command.occurrence_id}</code>")
    if command.notification_kind is NotificationKind.REPEAT:
        lines.append(f"Остановить повторы: <code>/ack {command.occurrence_id}</code>")
    if command.late:
        lines.insert(0, "⚠️ Доставлено с опозданием")
    return "\n".join(lines)


def notification_keyboard(command: DeliveryCommand) -> InlineKeyboardMarkup:
    return notification_keyboard_for_id(command.notification_id)


def notification_keyboard_for_id(notification_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Выполнено", callback_data=f"n:done:{notification_id}"),
                InlineKeyboardButton(text="Через час", callback_data=f"n:snooze:{notification_id}"),
            ],
            [
                InlineKeyboardButton(text="Перенести", callback_data=f"n:move:{notification_id}"),
                InlineKeyboardButton(text="Пропустить", callback_data=f"n:skip:{notification_id}"),
            ],
        ]
    )


def move_keyboard(
    notification_id: int,
    *,
    target_minute: int,
    target_label: str,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=target_label,
                    callback_data=f"n:at:{target_minute}:{notification_id}",
                )
            ],
            [InlineKeyboardButton(text="Назад", callback_data=f"n:back:{notification_id}")],
        ]
    )


__all__ = [
    "AiogramNotificationTransport",
    "move_keyboard",
    "notification_keyboard",
    "notification_keyboard_for_id",
    "notification_text",
]
