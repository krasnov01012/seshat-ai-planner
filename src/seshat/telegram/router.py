"""Тонкий aiogram-адаптер создания записей этапа 2."""

from __future__ import annotations

import datetime as dt
import html
import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from seshat.db.base import session_scope
from seshat.domain import DomainError, get_or_create_user, get_settings
from seshat.telegram.contracts import (
    Clarification,
    ManualEntryInput,
    ManualFallback,
    Ready,
    TelegramDependencies,
    as_json_object,
    resolve,
)
from seshat.telegram.keyboards import (
    confirmation_keyboard,
    date_keyboard,
    duration_keyboard,
    edit_keyboard,
    fallback_keyboard,
    kind_keyboard,
    main_menu,
    recurrence_keyboard,
    time_keyboard,
)
from seshat.telegram.presenters import confirmation_card, manual_fields_from_normalized
from seshat.telegram.states import Confirmation, ManualForm, TextClarification

log = logging.getLogger(__name__)


class OwnerOnly(Filter):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(self, event: TelegramObject) -> bool:
        user = getattr(event, "from_user", None)
        allowed = user is not None and user.id == self.owner_id
        if not allowed:
            log.warning("отклонён Telegram update от постороннего")
        return allowed


def build_router(owner_id: int, dependencies: TelegramDependencies) -> Router:
    router = Router(name="entry-creation")
    owner = OwnerOnly(owner_id)
    router.message.filter(owner)
    router.callback_query.filter(owner)

    @router.message(Command("start"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        log.info("получен /start от владельца", extra={"extra_fields": {"chat": message.chat.id}})
        await message.answer(
            "Сешат на связи. Напиши план обычным текстом или нажми «Добавить».",
            reply_markup=main_menu(),
        )

    @router.message(Command("add"))
    @router.message(StateFilter(None), F.text.casefold() == "добавить")
    async def add(message: Message, state: FSMContext) -> None:
        await _begin_manual(message, state, dependencies, owner_id)

    @router.callback_query(F.data == "manual:start")
    async def add_from_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message is not None:
            await _begin_manual(callback.message, state, dependencies, owner_id)

    @router.callback_query(F.data == "common:cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        if callback.message is not None:
            await callback.message.answer("Отменено.", reply_markup=main_menu())

    @router.message(Command("cancel"))
    async def cancel_message(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())

    @router.callback_query(ManualForm.kind, F.data.startswith("manual:kind:"))
    async def choose_kind(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        kind = (callback.data or "").rsplit(":", 1)[-1]
        if kind not in {"event", "task", "routine"}:
            return
        await state.update_data(kind=kind)
        await state.set_state(ManualForm.date)
        if callback.message is not None:
            await callback.message.answer(
                "Выбери дату или введи её в формате ДД.ММ.ГГГГ.",
                reply_markup=date_keyboard(),
            )

    @router.callback_query(ManualForm.date, F.data.startswith("manual:date:"))
    async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
        token = (callback.data or "").rsplit(":", 1)[-1]
        if token not in {"today", "tomorrow"}:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        await callback.answer()
        data = await state.get_data()
        today = _local_now(dependencies, str(data["tz"])).date()
        chosen = today if token == "today" else today + dt.timedelta(days=1)
        await state.update_data(local_date=chosen.isoformat())
        await state.set_state(ManualForm.time)
        if callback.message is not None:
            await callback.message.answer(
                "Укажи время в формате ЧЧ:ММ.", reply_markup=time_keyboard()
            )

    @router.message(ManualForm.date, F.text)
    async def enter_date(message: Message, state: FSMContext) -> None:
        try:
            chosen = _parse_date(message.text or "")
        except ValueError:
            await message.answer("Не понял дату. Используй формат ДД.ММ.ГГГГ.")
            return
        await state.update_data(local_date=chosen.isoformat())
        await state.set_state(ManualForm.time)
        await message.answer("Укажи время в формате ЧЧ:ММ.", reply_markup=time_keyboard())

    @router.callback_query(ManualForm.time, F.data.startswith("manual:time:"))
    async def choose_time(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        value = (callback.data or "").removeprefix("manual:time:")
        await _accept_time(value, state)
        if callback.message is not None:
            await _prompt_after_time(callback.message, state)

    @router.message(ManualForm.time, F.text)
    async def enter_time(message: Message, state: FSMContext) -> None:
        try:
            value = _parse_time(message.text or "").isoformat(timespec="minutes")
        except ValueError:
            await message.answer("Не понял время. Используй формат ЧЧ:ММ.")
            return
        await _accept_time(value, state)
        await _prompt_after_time(message, state)

    @router.callback_query(ManualForm.duration, F.data.startswith("manual:dur:"))
    async def choose_duration(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        token = (callback.data or "").rsplit(":", 1)[-1]
        await state.update_data(duration_min=None if token == "none" else int(token))
        await state.set_state(ManualForm.title)
        if callback.message is not None:
            await callback.message.answer("Как назвать запись?")

    @router.message(ManualForm.duration, F.text)
    async def enter_duration(message: Message, state: FSMContext) -> None:
        try:
            minutes = _parse_duration(message.text or "")
        except ValueError:
            await message.answer("Введи длительность в минутах, от 1 до 1440.")
            return
        await state.update_data(duration_min=minutes)
        await state.set_state(ManualForm.title)
        await message.answer("Как назвать запись?")

    @router.callback_query(ManualForm.recurrence, F.data.startswith("manual:rec:"))
    async def choose_recurrence(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        token = (callback.data or "").rsplit(":", 1)[-1]
        recurrence = _recurrence(token)
        await state.update_data(recurrence=recurrence)
        await state.set_state(ManualForm.title)
        if callback.message is not None:
            await callback.message.answer("Как назвать рутину?")

    @router.message(ManualForm.recurrence, F.text)
    async def enter_recurrence(message: Message, state: FSMContext) -> None:
        try:
            recurrence = _parse_recurrence(message.text or "")
        except ValueError:
            await message.answer("Напиши «каждый день», «по будням» или дни: пн, ср, пт.")
            return
        await state.update_data(recurrence=recurrence)
        await state.set_state(ManualForm.title)
        await message.answer("Как назвать рутину?")

    @router.message(ManualForm.title, F.text)
    async def enter_title(message: Message, state: FSMContext) -> None:
        title = " ".join((message.text or "").split())
        if not title or len(title) > 500:
            await message.answer("Название должно содержать от 1 до 500 символов.")
            return
        await state.update_data(title=title)
        await _prepare_manual_and_show(message, state, dependencies)

    @router.callback_query(Confirmation.ready, F.data.startswith("confirm:create:"))
    async def confirm_create(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if not _nonce_matches(callback.data, data):
            await callback.answer("Карточка устарела.", show_alert=True)
            return
        await callback.answer()
        confirmation_id = str(data["nonce"])
        try:
            async with session_scope(dependencies.session_factory) as session:
                result = await dependencies.create_entry(
                    session,
                    int(data["user_id"]),
                    data["normalized"],
                    confirmation_id,
                )
        except (DomainError, SQLAlchemyError, ValueError):
            log.exception("не удалось сохранить подтверждённую запись")
            if callback.message is not None:
                await callback.message.answer(
                    "Не удалось сохранить. Карточка осталась активной — попробуй ещё раз."
                )
            return
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            entry = getattr(result, "entry", result)
            entry_id = getattr(entry, "id", None)
            suffix = f" #{entry_id}" if entry_id is not None else ""
            await callback.message.answer(f"Запись{suffix} создана.", reply_markup=main_menu())

    @router.callback_query(Confirmation.ready, F.data.startswith("confirm:cancel:"))
    async def confirm_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if not _nonce_matches(callback.data, data):
            await callback.answer("Карточка устарела.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Создание отменено.", reply_markup=main_menu())

    @router.callback_query(Confirmation.ready, F.data.startswith("confirm:edit:"))
    async def confirm_edit(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if not _nonce_matches(callback.data, data):
            await callback.answer("Карточка устарела.", show_alert=True)
            return
        await callback.answer()
        if callback.message is not None:
            manual = data["manual"]
            await callback.message.answer(
                "Что изменить?",
                reply_markup=edit_keyboard(
                    str(data["nonce"]), routine=manual.get("kind") == "routine"
                ),
            )

    @router.callback_query(Confirmation.ready, F.data.startswith("edit:"))
    async def choose_edit(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if not _nonce_matches(callback.data, data):
            await callback.answer("Карточка устарела.", show_alert=True)
            return
        await callback.answer()
        field = (callback.data or "").split(":")[1]
        if field == "back":
            if callback.message is not None:
                await _show_existing(callback.message, state)
            return
        state_by_field = {
            "kind": Confirmation.edit_kind,
            "date": Confirmation.edit_date,
            "time": Confirmation.edit_time,
            "duration": Confirmation.edit_duration,
            "recurrence": Confirmation.edit_recurrence,
            "title": Confirmation.edit_title,
            "reminders": Confirmation.edit_reminders,
        }
        target = state_by_field.get(field)
        if target is None or callback.message is None:
            return
        await state.set_state(target)
        await _prompt_edit(callback.message, field)

    @router.callback_query(Confirmation.edit_kind, F.data.startswith("change:kind:"))
    async def edit_kind_value(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        kind = (callback.data or "").rsplit(":", 1)[-1]
        data = await state.get_data()
        manual = dict(data["manual"])
        manual["kind"] = kind
        manual["recurrence"] = manual.get("recurrence") if kind == "routine" else None
        await state.update_data(manual=manual)
        if kind == "routine" and manual.get("recurrence") is None:
            await state.set_state(Confirmation.edit_recurrence)
            if callback.message is not None:
                await callback.message.answer(
                    "Выбери повторение.", reply_markup=recurrence_keyboard("change")
                )
            return
        if callback.message is not None:
            await _reprepare_and_show(callback.message, state, dependencies)

    @router.callback_query(Confirmation.edit_date, F.data.startswith("change:date:"))
    async def edit_date_callback(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        today = _local_now(dependencies, str(data["tz"])).date()
        token = (callback.data or "").rsplit(":", 1)[-1]
        if token not in {"today", "tomorrow"}:
            await callback.answer("Кнопка устарела.", show_alert=True)
            return
        await callback.answer()
        chosen = today if token == "today" else today + dt.timedelta(days=1)
        await _update_manual(state, local_date=chosen.isoformat())
        if callback.message is not None:
            await _reprepare_and_show(callback.message, state, dependencies)

    @router.callback_query(Confirmation.edit_time, F.data.startswith("change:time:"))
    async def edit_time_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await _update_manual(state, local_time=(callback.data or "").removeprefix("change:time:"))
        if callback.message is not None:
            await _reprepare_and_show(callback.message, state, dependencies)

    @router.callback_query(Confirmation.edit_duration, F.data.startswith("change:dur:"))
    async def edit_duration_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        token = (callback.data or "").rsplit(":", 1)[-1]
        await _update_manual(state, duration_min=None if token == "none" else int(token))
        if callback.message is not None:
            await _reprepare_and_show(callback.message, state, dependencies)

    @router.callback_query(Confirmation.edit_recurrence, F.data.startswith("change:rec:"))
    async def edit_recurrence_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await _update_manual(
            state, recurrence=_recurrence((callback.data or "").rsplit(":", 1)[-1])
        )
        if callback.message is not None:
            await _reprepare_and_show(callback.message, state, dependencies)

    @router.message(Confirmation.edit_date, F.text)
    async def edit_date_text(message: Message, state: FSMContext) -> None:
        try:
            chosen = _parse_date(message.text or "")
        except ValueError:
            await message.answer("Не понял дату. Используй формат ДД.ММ.ГГГГ.")
            return
        await _update_manual(state, local_date=chosen.isoformat())
        await _reprepare_and_show(message, state, dependencies)

    @router.message(Confirmation.edit_time, F.text)
    async def edit_time_text(message: Message, state: FSMContext) -> None:
        try:
            value = _parse_time(message.text or "").isoformat(timespec="minutes")
        except ValueError:
            await message.answer("Не понял время. Используй формат ЧЧ:ММ.")
            return
        await _update_manual(state, local_time=value)
        await _reprepare_and_show(message, state, dependencies)

    @router.message(Confirmation.edit_duration, F.text)
    async def edit_duration_text(message: Message, state: FSMContext) -> None:
        try:
            value = _parse_duration(message.text or "")
        except ValueError:
            await message.answer("Введи длительность в минутах, от 1 до 1440.")
            return
        await _update_manual(state, duration_min=value)
        await _reprepare_and_show(message, state, dependencies)

    @router.message(Confirmation.edit_recurrence, F.text)
    async def edit_recurrence_text(message: Message, state: FSMContext) -> None:
        try:
            value = _parse_recurrence(message.text or "")
        except ValueError:
            await message.answer("Напиши «каждый день», «по будням» или дни: пн, ср, пт.")
            return
        await _update_manual(state, recurrence=value)
        await _reprepare_and_show(message, state, dependencies)

    @router.message(Confirmation.edit_title, F.text)
    async def edit_title_text(message: Message, state: FSMContext) -> None:
        title = " ".join((message.text or "").split())
        if not title or len(title) > 500:
            await message.answer("Название должно содержать от 1 до 500 символов.")
            return
        await _update_manual(state, title=title)
        await _reprepare_and_show(message, state, dependencies)

    @router.message(Confirmation.edit_reminders, F.text)
    async def edit_reminders_text(message: Message, state: FSMContext) -> None:
        try:
            values = _parse_reminders(message.text or "")
        except ValueError:
            await message.answer("Введи до пяти минут через запятую, например: 1440, 60.")
            return
        await _update_manual(state, reminders_min_before=values)
        await _reprepare_and_show(message, state, dependencies)

    @router.message(StateFilter(None), F.reply_to_message)
    async def unknown_reminder_reply(message: Message) -> None:
        await message.answer("Не нашёл напоминание, на которое ты ответил.")

    @router.message(StateFilter(None), F.text)
    async def natural_text(message: Message, state: FSMContext) -> None:
        await _prepare_text_and_route(message, state, dependencies, owner_id, message.text or "")

    @router.message(TextClarification.waiting, F.text)
    async def clarification_answer(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        original = str(data.get("original_text") or "")
        combined = f"Исходный план: {original}\nУточнение пользователя: {message.text or ''}"
        await _prepare_text_and_route(message, state, dependencies, owner_id, combined)

    @router.callback_query(F.data.startswith(("confirm:", "edit:", "change:")))
    async def stale_callback(callback: CallbackQuery) -> None:
        await callback.answer("Карточка устарела.", show_alert=True)

    return router


async def _begin_manual(
    message: Message,
    state: FSMContext,
    dependencies: TelegramDependencies,
    owner_id: int,
) -> None:
    await state.clear()
    async with session_scope(dependencies.session_factory) as session:
        user = await get_or_create_user(session, owner_id, default_tz=dependencies.default_tz)
        settings = await get_settings(session, user.id)
        user_id, tz = user.id, settings.tz
    await state.update_data(user_id=user_id, tz=tz, manual={})
    await state.set_state(ManualForm.kind)
    await message.answer("Что создаём?", reply_markup=kind_keyboard())


async def _accept_time(value: str, state: FSMContext) -> None:
    _parse_time(value)
    await state.update_data(local_time=value)


async def _prompt_after_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data["kind"] == "routine":
        await state.set_state(ManualForm.recurrence)
        await message.answer(
            "Как повторять? Можно также написать дни: пн, ср, пт.",
            reply_markup=recurrence_keyboard(),
        )
    else:
        await state.set_state(ManualForm.duration)
        await message.answer(
            "Укажи длительность в минутах или выбери вариант.",
            reply_markup=duration_keyboard(),
        )


async def _prepare_manual_and_show(
    message: Message, state: FSMContext, dependencies: TelegramDependencies
) -> None:
    data = await state.get_data()
    manual = _manual_input(data)
    try:
        prepared = await resolve(
            dependencies.prepare_manual(manual, str(data["tz"]), dependencies.clock())
        )
    except (DomainError, ValidationError, ValueError) as exc:
        await state.set_state(ManualForm.date)
        await message.answer(
            f"Не удалось подготовить карточку: {html.escape(str(exc))}\nВыбери корректную дату.",
            reply_markup=date_keyboard(),
        )
        return
    payload = as_json_object(prepared)
    await _store_and_show(message, state, payload, manual_data=_manual_data(data))


async def _reprepare_and_show(
    message: Message, state: FSMContext, dependencies: TelegramDependencies
) -> None:
    data = await state.get_data()
    manual = dict(data["manual"])
    merged = {**data, **manual}
    try:
        prepared = await resolve(
            dependencies.prepare_manual(
                _manual_input(merged), str(data["tz"]), dependencies.clock()
            )
        )
    except (DomainError, ValidationError, ValueError) as exc:
        await message.answer(
            f"Не удалось обновить карточку: {html.escape(str(exc))}. Исправь поле ещё раз."
        )
        return
    await _store_and_show(message, state, as_json_object(prepared), manual_data=manual)


async def _store_and_show(
    message: Message,
    state: FSMContext,
    payload: dict[str, Any],
    *,
    manual_data: dict[str, Any] | None = None,
) -> None:
    nonce = str(uuid.uuid4())
    manual = manual_data or manual_fields_from_normalized(payload)
    await state.update_data(normalized=payload, nonce=nonce, manual=manual)
    await state.set_state(Confirmation.ready)
    await message.answer(confirmation_card(payload), reply_markup=confirmation_keyboard(nonce))


async def _show_existing(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(Confirmation.ready)
    await message.answer(
        confirmation_card(data["normalized"]),
        reply_markup=confirmation_keyboard(str(data["nonce"])),
    )


async def _prepare_text_and_route(
    message: Message,
    state: FSMContext,
    dependencies: TelegramDependencies,
    owner_id: int,
    text: str,
) -> None:
    async with session_scope(dependencies.session_factory) as session:
        user = await get_or_create_user(session, owner_id, default_tz=dependencies.default_tz)
        settings = await get_settings(session, user.id)
        user_id, tz = user.id, settings.tz
    # Пользователь должен закоммититься до отдельной сессии записи ai_calls,
    # иначе PostgreSQL не сможет проверить внешний ключ.
    async with session_scope(dependencies.session_factory) as session:
        result = await dependencies.prepare_text(
            session,
            user_id,
            text,
            tz=tz,
            now_utc=dependencies.clock(),
        )
    if isinstance(result, Ready):
        await state.update_data(user_id=user_id, tz=tz)
        await _store_and_show(message, state, as_json_object(result))
    elif isinstance(result, Clarification):
        await state.set_state(TextClarification.waiting)
        await state.update_data(original_text=text)
        await message.answer(result.prompt, reply_markup=fallback_keyboard())
    elif isinstance(result, ManualFallback):
        await state.clear()
        await message.answer(result.prompt, reply_markup=fallback_keyboard())
    else:
        raise TypeError("prepare_text returned an unsupported result")


async def _prompt_edit(message: Message, field: str) -> None:
    if field == "kind":
        await message.answer("Выбери новый тип.", reply_markup=kind_keyboard("change"))
    elif field == "date":
        await message.answer("Введи дату ДД.ММ.ГГГГ.", reply_markup=date_keyboard("change"))
    elif field == "time":
        await message.answer("Введи время ЧЧ:ММ.", reply_markup=time_keyboard("change"))
    elif field == "duration":
        await message.answer("Введи минуты.", reply_markup=duration_keyboard("change"))
    elif field == "recurrence":
        await message.answer("Выбери повторение.", reply_markup=recurrence_keyboard("change"))
    elif field == "title":
        await message.answer("Введи новое название.")
    elif field == "reminders":
        await message.answer("Минуты до начала через запятую или «нет».")


async def _update_manual(state: FSMContext, **changes: Any) -> None:
    data = await state.get_data()
    manual = dict(data["manual"])
    manual.update(changes)
    await state.update_data(manual=manual)


def _manual_input(data: dict[str, Any]) -> ManualEntryInput:
    return ManualEntryInput(
        kind=str(data["kind"]),
        title=str(data["title"]),
        local_date=dt.date.fromisoformat(str(data["local_date"])),
        local_time=_parse_time(str(data["local_time"])),
        duration_min=data.get("duration_min"),
        recurrence=data.get("recurrence"),
        reminders_min_before=tuple(data.get("reminders_min_before") or ()),
    )


def _manual_data(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": data["kind"],
        "title": data["title"],
        "local_date": data["local_date"],
        "local_time": data["local_time"],
        "duration_min": data.get("duration_min"),
        "recurrence": data.get("recurrence"),
        "reminders_min_before": list(data.get("reminders_min_before") or []),
    }


def _parse_time(value: str) -> dt.time:
    hour, minute = (int(part) for part in value.strip().split(":"))
    return dt.time(hour=hour, minute=minute)


def _parse_date(value: str) -> dt.date:
    day, month, year = (int(part) for part in value.strip().split("."))
    return dt.date(year=year, month=month, day=day)


def _parse_duration(value: str) -> int:
    minutes = int(value.strip())
    if not 1 <= minutes <= 1440:
        raise ValueError
    return minutes


def _recurrence(token: str) -> dict[str, Any]:
    if token == "daily":
        return {"freq": "daily", "byweekday": None, "interval": 1}
    if token == "weekdays":
        return {
            "freq": "weekly",
            "byweekday": ["mon", "tue", "wed", "thu", "fri"],
            "interval": 1,
        }
    raise ValueError("unknown recurrence")


def _parse_recurrence(value: str) -> dict[str, Any]:
    cleaned = value.casefold().strip()
    if cleaned in {"каждый день", "ежедневно"}:
        return _recurrence("daily")
    if cleaned in {"по будням", "будни"}:
        return _recurrence("weekdays")
    aliases = {
        "пн": "mon",
        "вт": "tue",
        "ср": "wed",
        "чт": "thu",
        "пт": "fri",
        "сб": "sat",
        "вс": "sun",
    }
    days = [aliases.get(part.strip()) for part in cleaned.split(",")]
    if not days or any(day is None for day in days):
        raise ValueError
    return {"freq": "weekly", "byweekday": list(dict.fromkeys(days)), "interval": 1}


def _parse_reminders(value: str) -> list[int]:
    if value.casefold().strip() in {"нет", "без", "0"}:
        # [] означает defaults. Ноль — явный sentinel «только основное»
        # в существующей схеме без дополнительной колонки.
        return [0]
    values = sorted({int(part.strip()) for part in value.split(",")}, reverse=True)
    if len(values) > 5 or any(item < 0 for item in values):
        raise ValueError
    return values


def _local_now(dependencies: TelegramDependencies, tz: str) -> dt.datetime:
    now = dependencies.clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Telegram clock must return tz-aware datetime")
    return now.astimezone(ZoneInfo(tz))


def _nonce_matches(callback_data: str | None, data: dict[str, Any]) -> bool:
    return bool(callback_data and callback_data.rsplit(":", 1)[-1] == data.get("nonce"))
