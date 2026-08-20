"""Тонкий Telegram-адаптер подтверждённой смены таймзоны."""

from __future__ import annotations

import html
import uuid

from aiogram import F, Router
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)

from seshat.config import Settings
from seshat.db.base import session_scope
from seshat.domain.scheduling import ReminderDefaults
from seshat.domain.timezones import (
    TimezoneChangePreview,
    TimezoneReviewDecision,
    TimezoneReviewItem,
    confirm_timezone_change,
    find_pending_timezone_change,
    list_timezone_reviews,
    preview_timezone_change,
    rebuild_timezone_horizon,
    review_timezone_entry,
)
from seshat.domain.users import DomainError, get_or_create_user
from seshat.telegram.contracts import TelegramDependencies
from seshat.telegram.keyboards import main_menu


class TimezoneFlow(StatesGroup):
    entering = State()
    confirming = State()
    reviewing = State()


class _OwnerOnly(Filter):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id == self.owner_id


def _choose_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Москва", callback_data="tz:pick:Europe/Moscow"),
                InlineKeyboardButton(text="Амстердам", callback_data="tz:pick:Europe/Amsterdam"),
            ],
            [InlineKeyboardButton(text="Отменить", callback_data="tz:cancel")],
        ]
    )


def _confirmation_keyboard(nonce: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=f"tz:confirm:{nonce}"),
                InlineKeyboardButton(text="Отменить", callback_data="tz:cancel"),
            ]
        ]
    )


def _review_keyboard(entry_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Оставить момент", callback_data=f"tz:keep:{entry_id}"),
                InlineKeyboardButton(
                    text="Оставить местное время", callback_data=f"tz:local:{entry_id}"
                ),
            ],
            [InlineKeyboardButton(text="Продолжить позже", callback_data="tz:later")],
        ]
    )


def _reminder_defaults(settings: Settings) -> ReminderDefaults:
    return ReminderDefaults(
        event_pre_min=settings.default_event_reminders_min,
        task_pre_min=settings.default_task_reminders_min,
        task_morning_local=settings.default_task_morning_local,
        routine_pre_min=settings.default_routine_reminders_min,
    )


def _preview_text(preview: TimezoneChangePreview) -> str:
    return (
        "<b>Сменить часовой пояс?</b>\n"
        f"{html.escape(preview.tz_from)} → {html.escape(preview.tz_to)}\n"
        f"Сейчас: {preview.now_from:%H:%M} → {preview.now_to:%H:%M}\n"
        f"Рутин будет пересчитано: {preview.routine_count}\n"
        f"Будущих событий и дедлайнов для разбора: {preview.review_count}\n\n"
        "Рутины сохранят местное время. События и дедлайны пока сохранят "
        "абсолютный момент."
    )


def _review_text(item: TimezoneReviewItem) -> str:
    return (
        f"<b>{html.escape(item.title)}</b>\n"
        f"Оставить момент — будет {item.keep_absolute_local:%d.%m.%Y %H:%M}.\n"
        f"Оставить местное время — будет {item.keep_local_local:%d.%m.%Y %H:%M}, "
        "абсолютный момент сдвинется."
    )


async def _owner_id(dependencies: TelegramDependencies, telegram_owner_id: int) -> int:
    async with session_scope(dependencies.session_factory) as session:
        user = await get_or_create_user(
            session, telegram_owner_id, default_tz=dependencies.default_tz
        )
        return user.id


async def _show_review(
    message: Message, state: FSMContext, change_id: int, item: TimezoneReviewItem
) -> None:
    await state.set_state(TimezoneFlow.reviewing)
    await state.update_data(tz_change_id=change_id, tz_entry_id=item.entry_id)
    await message.answer(_review_text(item), reply_markup=_review_keyboard(item.entry_id))


def build_timezone_router(
    owner_id: int, dependencies: TelegramDependencies, settings: Settings
) -> Router:
    router = Router(name="timezone-change")
    owner = _OwnerOnly(owner_id)
    router.message.filter(owner)
    router.callback_query.filter(owner)

    async def begin(message: Message, state: FSMContext) -> None:
        user_id = await _owner_id(dependencies, owner_id)
        async with session_scope(dependencies.session_factory) as session:
            pending = await find_pending_timezone_change(session, user_id)
            reviews = await list_timezone_reviews(session, user_id, pending.id) if pending else []
        if pending is not None and reviews:
            await state.update_data(tz_user_id=user_id)
            await message.answer("Сначала закончим разбор после предыдущего переезда.")
            await _show_review(message, state, pending.id, reviews[0])
            return
        await state.set_state(TimezoneFlow.entering)
        await state.update_data(tz_user_id=user_id)
        await message.answer(
            "Введи IANA-таймзону, например Europe/Amsterdam, или выбери вариант.",
            reply_markup=_choose_keyboard(),
        )

    async def prepare(message: Message, state: FSMContext, new_tz: str) -> None:
        data = await state.get_data()
        user_id = int(data["tz_user_id"])
        try:
            async with session_scope(dependencies.session_factory) as session:
                preview = await preview_timezone_change(
                    session,
                    user_id,
                    new_tz.strip(),
                    now_utc=dependencies.clock(),
                )
        except DomainError as exc:
            await message.answer(f"Не удалось подготовить смену: {html.escape(str(exc))}")
            return
        nonce = str(preview.confirmation_id)
        await state.set_state(TimezoneFlow.confirming)
        await state.update_data(
            tz_nonce=nonce,
            tz_from=preview.tz_from,
            tz_to=preview.tz_to,
        )
        await message.answer(_preview_text(preview), reply_markup=_confirmation_keyboard(nonce))

    @router.message(Command("timezone"))
    @router.message(StateFilter(None), F.text.casefold() == "настройки")
    async def start_timezone(message: Message, state: FSMContext) -> None:
        await state.clear()
        await begin(message, state)

    @router.callback_query(TimezoneFlow.entering, F.data.startswith("tz:pick:"))
    async def choose_timezone(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message is not None:
            await prepare(
                callback.message,
                state,
                (callback.data or "").removeprefix("tz:pick:"),
            )

    @router.message(TimezoneFlow.entering, F.text)
    async def enter_timezone(message: Message, state: FSMContext) -> None:
        await prepare(message, state, message.text or "")

    @router.callback_query(TimezoneFlow.confirming, F.data.startswith("tz:confirm:"))
    async def confirm_timezone(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        nonce = (callback.data or "").removeprefix("tz:confirm:")
        if nonce != data.get("tz_nonce"):
            await callback.answer("Карточка устарела.", show_alert=True)
            return
        await callback.answer()
        try:
            async with session_scope(dependencies.session_factory) as session:
                result = await confirm_timezone_change(
                    session,
                    int(data["tz_user_id"]),
                    str(data["tz_to"]),
                    expected_tz_from=str(data["tz_from"]),
                    confirmation_id=uuid.UUID(nonce),
                    now_utc=dependencies.clock(),
                )
                await rebuild_timezone_horizon(
                    session,
                    int(data["tz_user_id"]),
                    now_utc=dependencies.clock(),
                    horizon_days=settings.materialize_horizon_days,
                    defaults=_reminder_defaults(settings),
                )
        except (DomainError, ValueError) as exc:
            if callback.message is not None:
                await callback.message.answer(
                    f"Не удалось сменить таймзону: {html.escape(str(exc))}"
                )
            return
        if callback.message is None:
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"Таймзона изменена: {html.escape(result.change.tz_to)}.")
        if result.next_review is not None:
            await _show_review(callback.message, state, result.change.id, result.next_review)
        else:
            await state.clear()
            await callback.message.answer("Разбор завершён.", reply_markup=main_menu())

    @router.callback_query(TimezoneFlow.reviewing, F.data.startswith(("tz:keep:", "tz:local:")))
    async def decide_review(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        entry_id = int((callback.data or "").rsplit(":", 1)[-1])
        if entry_id != data.get("tz_entry_id"):
            await callback.answer("Карточка устарела.", show_alert=True)
            return
        decision = (
            TimezoneReviewDecision.KEEP_LOCAL
            if (callback.data or "").startswith("tz:local:")
            else TimezoneReviewDecision.KEEP_ABSOLUTE
        )
        await callback.answer()
        try:
            async with session_scope(dependencies.session_factory) as session:
                result = await review_timezone_entry(
                    session,
                    int(data["tz_user_id"]),
                    int(data["tz_change_id"]),
                    entry_id,
                    decision,
                    now_utc=dependencies.clock(),
                )
                await rebuild_timezone_horizon(
                    session,
                    int(data["tz_user_id"]),
                    now_utc=dependencies.clock(),
                    horizon_days=settings.materialize_horizon_days,
                    defaults=_reminder_defaults(settings),
                )
        except DomainError as exc:
            if callback.message is not None:
                await callback.message.answer(
                    f"Не удалось сохранить выбор: {html.escape(str(exc))}"
                )
            return
        if callback.message is None:
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        if result.next_review is not None:
            await _show_review(callback.message, state, result.change_id, result.next_review)
        else:
            await state.clear()
            await callback.message.answer("Все будущие записи разобраны.", reply_markup=main_menu())

    @router.callback_query(F.data == "tz:later")
    async def review_later(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer(
                "Хорошо. Продолжить можно через /timezone.", reply_markup=main_menu()
            )

    @router.callback_query(F.data == "tz:cancel")
    async def cancel_timezone(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Смена таймзоны отменена.", reply_markup=main_menu())

    return router


__all__ = ["TimezoneFlow", "build_timezone_router"]
