"""Telegram-сценарии одного occurrence, полностью без AI."""

from __future__ import annotations

import datetime as dt
import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, Message

from seshat.config import Settings
from seshat.db.base import session_scope
from seshat.domain import DomainError, get_or_create_user
from seshat.domain.delivery import ActiveReactionResult, acknowledge_occurrence
from seshat.domain.reactions import (
    complete_from_notification,
    move_from_notification,
    preview_move_tomorrow,
    skip_from_notification,
    snooze_from_notification,
)
from seshat.domain.scheduling import ReminderDefaults, skip_occurrence
from seshat.telegram.contracts import TelegramDependencies
from seshat.telegram.delivery import move_keyboard, notification_keyboard_for_id
from seshat.telegram.router import OwnerOnly


class ResolvedReactionContext(Filter):
    async def __call__(
        self,
        message: Message,
        reaction_context: ActiveReactionResult | None = None,
    ) -> bool:
        del message
        return reaction_context is not None and reaction_context.reacted


def build_occurrence_router(
    owner_id: int,
    dependencies: TelegramDependencies,
    settings: Settings,
) -> Router:
    router = Router(name="occurrence-actions")
    owner = OwnerOnly(owner_id)
    router.message.filter(owner)
    router.callback_query.filter(owner)

    def defaults() -> ReminderDefaults:
        return ReminderDefaults(
            event_pre_min=settings.default_event_reminders_min,
            task_pre_min=settings.default_task_reminders_min,
            task_morning_local=settings.default_task_morning_local,
            routine_pre_min=settings.default_routine_reminders_min,
        )

    async def owner_user_id(session) -> int:
        user = await get_or_create_user(
            session,
            owner_id,
            default_tz=dependencies.default_tz,
        )
        return user.id

    async def finish_callback(callback: CallbackQuery, text: str) -> None:
        await callback.answer(text)
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("n:done:"))
    async def complete_callback(callback: CallbackQuery) -> None:
        notification_id = _callback_notification_id(callback.data)
        if notification_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user_id = await owner_user_id(session)
                await complete_from_notification(
                    session,
                    user_id,
                    notification_id,
                    reacted_at_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await finish_callback(callback, "Отмечено выполненным.")

    @router.callback_query(F.data.startswith("n:snooze:"))
    async def snooze_callback(callback: CallbackQuery) -> None:
        notification_id = _callback_notification_id(callback.data)
        if notification_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user_id = await owner_user_id(session)
                await snooze_from_notification(
                    session,
                    user_id,
                    notification_id,
                    reacted_at_utc=dependencies.clock(),
                    minutes=60,
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await finish_callback(callback, "Напомню через час.")

    @router.callback_query(F.data.startswith("n:skip:"))
    async def generic_skip_callback(callback: CallbackQuery) -> None:
        notification_id = _callback_notification_id(callback.data)
        if notification_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user_id = await owner_user_id(session)
                await skip_from_notification(
                    session,
                    user_id,
                    notification_id,
                    reacted_at_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await finish_callback(callback, "Этот экземпляр пропущен.")

    @router.callback_query(F.data.startswith("n:move:"))
    async def preview_move_callback(callback: CallbackQuery) -> None:
        notification_id = _callback_notification_id(callback.data)
        if notification_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user_id = await owner_user_id(session)
                preview = await preview_move_tomorrow(
                    session,
                    user_id,
                    notification_id,
                    now_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await callback.answer()
        if callback.message is not None:
            await callback.message.edit_reply_markup(
                reply_markup=move_keyboard(
                    notification_id,
                    target_minute=int(preview.target_at_utc.timestamp() // 60),
                    target_label=f"На {preview.target_local:%d.%m %H:%M}",
                )
            )

    @router.callback_query(F.data.startswith("n:at:"))
    async def apply_move_callback(callback: CallbackQuery) -> None:
        parsed = _move_callback(callback.data)
        if parsed is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        target_minute, notification_id = parsed
        target = dt.datetime.fromtimestamp(target_minute * 60, tz=dt.UTC)
        try:
            async with session_scope(dependencies.session_factory) as session:
                user_id = await owner_user_id(session)
                await move_from_notification(
                    session,
                    user_id,
                    notification_id,
                    target,
                    reacted_at_utc=dependencies.clock(),
                    defaults=defaults(),
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await finish_callback(callback, "Перенесено.")

    @router.callback_query(F.data.startswith("n:back:"))
    async def move_back_callback(callback: CallbackQuery) -> None:
        notification_id = _callback_notification_id(callback.data)
        if notification_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        await callback.answer()
        if callback.message is not None:
            await callback.message.edit_reply_markup(
                reply_markup=notification_keyboard_for_id(notification_id)
            )

    @router.callback_query(F.data.startswith("notify:ack:"))
    async def acknowledge_callback(callback: CallbackQuery) -> None:
        occurrence_id = _callback_occurrence_id(callback.data)
        if occurrence_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user = await get_or_create_user(
                    session,
                    owner_id,
                    default_tz=dependencies.default_tz,
                )
                await acknowledge_occurrence(
                    session,
                    user.id,
                    occurrence_id,
                    reacted_at_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await callback.answer("Повторы остановлены.")
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.callback_query(F.data.startswith("notify:skip:"))
    async def skip_callback(callback: CallbackQuery) -> None:
        occurrence_id = _callback_occurrence_id(callback.data)
        if occurrence_id is None:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user = await get_or_create_user(
                    session,
                    owner_id,
                    default_tz=dependencies.default_tz,
                )
                await skip_occurrence(
                    session,
                    user_id=user.id,
                    occurrence_id=occurrence_id,
                    now_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await callback.answer(html.escape(str(exc)), show_alert=True)
            return
        await callback.answer("Этот экземпляр пропущен.")
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(Command("skip"))
    async def skip_one(message: Message, command: CommandObject) -> None:
        token = (command.args or "").strip()
        if not token.isdecimal():
            await message.answer("Укажи номер экземпляра: <code>/skip 123</code>.")
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user = await get_or_create_user(
                    session,
                    owner_id,
                    default_tz=dependencies.default_tz,
                )
                result = await skip_occurrence(
                    session,
                    user_id=user.id,
                    occurrence_id=int(token),
                    now_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await message.answer(f"Не удалось пропустить: {html.escape(str(exc))}")
            return
        text = "Этот экземпляр рутины пропущен." if result.changed else "Он уже был пропущен."
        await message.answer(text)

    @router.message(Command("ack"))
    async def acknowledge(message: Message, command: CommandObject) -> None:
        token = (command.args or "").strip()
        if not token.isdecimal():
            await message.answer("Укажи номер напоминания: <code>/ack 123</code>.")
            return
        try:
            async with session_scope(dependencies.session_factory) as session:
                user = await get_or_create_user(
                    session,
                    owner_id,
                    default_tz=dependencies.default_tz,
                )
                result = await acknowledge_occurrence(
                    session,
                    user.id,
                    int(token),
                    reacted_at_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await message.answer(f"Не удалось остановить повторы: {html.escape(str(exc))}")
            return
        await message.answer(
            "Повторы остановлены."
            if result.cancelled
            else "Активных повторов для этого напоминания уже нет."
        )

    @router.message(F.reply_to_message, ResolvedReactionContext())
    async def reminder_reply(
        message: Message,
        reaction_context: ActiveReactionResult | None = None,
    ) -> None:
        if reaction_context is not None and reaction_context.reacted:
            await message.answer("Контекст напоминания выбран. Используй кнопки под ним.")
            return
        await message.answer("Не нашёл напоминание, на которое ты ответил.")

    return router


def _callback_occurrence_id(data: str | None) -> int | None:
    token = (data or "").rsplit(":", 1)[-1]
    return int(token) if token.isdecimal() else None


def _callback_notification_id(data: str | None) -> int | None:
    token = (data or "").rsplit(":", 1)[-1]
    return int(token) if token.isdecimal() else None


def _move_callback(data: str | None) -> tuple[int, int] | None:
    parts = (data or "").split(":")
    if len(parts) != 4 or parts[:2] != ["n", "at"]:
        return None
    if not parts[2].isdecimal() or not parts[3].isdecimal():
        return None
    return int(parts[2]), int(parts[3])


__all__ = ["ResolvedReactionContext", "build_occurrence_router"]
