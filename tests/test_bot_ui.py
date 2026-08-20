"""Быстрые unit-тесты Telegram-адаптера этапа 2."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from seshat.bot import build_dependencies, build_dispatcher
from seshat.config import Settings
from seshat.db.enums import EntryKind, NotificationKind
from seshat.domain.ai import (
    ManualFallbackReason,
    PreparationSource,
    TextManualFallback,
    TextReady,
)
from seshat.domain.delivery import ActiveReactionResult, DeliveryCommand
from seshat.domain.nim import NimResult
from seshat.domain.parsing import Intent, NormalizedEntry, ParsedPlan
from seshat.telegram.contracts import ManualEntryInput, ManualFallback, Ready, resolve
from seshat.telegram.delivery import move_keyboard, notification_keyboard
from seshat.telegram.keyboards import confirmation_keyboard, edit_keyboard, main_menu
from seshat.telegram.occurrences import ResolvedReactionContext
from seshat.telegram.presenters import confirmation_card, manual_fields_from_normalized
from seshat.telegram.reactions import (
    ActiveReactionMiddleware,
    event_actor_id,
    reply_to_message_id,
)
from seshat.telegram.router import (
    _nonce_matches,
    _parse_recurrence,
    _parse_reminders,
    _prompt_after_time,
    _store_and_show,
    build_router,
)
from seshat.telegram.states import Confirmation, ManualForm

NOW = dt.datetime(2026, 8, 3, 12, tzinfo=dt.UTC)


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="123456789:" + "AAtest-token-value-for-tests-only-xxxx",
        telegram_owner_id=123456789,
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        env="test",
    )


class FakeTextService:
    def __init__(self, *, fallback: bool = False) -> None:
        self.fallback = fallback
        self.calls = 0

    async def prepare_text(self, *_: object, **__: object) -> TextReady | TextManualFallback:
        self.calls += 1
        if self.fallback:
            return TextManualFallback(ManualFallbackReason.AI_UNAVAILABLE)
        plan = ParsedPlan(
            intent=Intent.CREATE,
            title="Созвон",
            start=dt.datetime(2026, 8, 4, 15),
            confidence=0.99,
        )
        return TextReady(
            entry=NormalizedEntry(
                kind="event",
                title="Созвон",
                start_at_utc=dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC),
                tz="Europe/Moscow",
                local_time=dt.time(15, 0),
            ),
            source=PreparationSource.AI,
            nim=NimResult(
                plan=plan,
                model="fake",
                latency_ms=1,
                prompt_tokens=1,
                completion_tokens=1,
                attempts=1,
                used_fallback=False,
            ),
        )


def _dependencies(service: FakeTextService):
    factory = cast(async_sessionmaker[AsyncSession], object())
    return build_dependencies(_settings(), factory, cast(object, service))


async def test_manual_prepare_never_calls_nim() -> None:
    service = FakeTextService()
    dependencies = _dependencies(service)
    manual = ManualEntryInput(
        kind="event",
        title="Собеседование",
        local_date=dt.date(2026, 8, 4),
        local_time=dt.time(15, 0),
        duration_min=60,
    )

    draft = await resolve(dependencies.prepare_manual(manual, "Europe/Moscow", NOW))

    assert draft.kind.value == "event"
    assert draft.start_at_utc == dt.datetime(2026, 8, 4, 12, tzinfo=dt.UTC)
    assert service.calls == 0


async def test_text_prepare_returns_typed_ready() -> None:
    service = FakeTextService()
    dependencies = _dependencies(service)

    result = await dependencies.prepare_text(
        cast(AsyncSession, object()),
        1,
        "Завтра в 15:00 созвон",
        tz="Europe/Moscow",
        now_utc=NOW,
    )

    assert isinstance(result, Ready)
    assert result.normalized["kind"] == "event"
    assert service.calls == 1


async def test_nim_unavailable_routes_to_manual_fallback() -> None:
    dependencies = _dependencies(FakeTextService(fallback=True))

    result = await dependencies.prepare_text(
        cast(AsyncSession, object()),
        1,
        "Завтра в 15:00 созвон",
        tz="Europe/Moscow",
        now_utc=NOW,
    )

    assert isinstance(result, ManualFallback)


def test_confirmation_card_is_localized_and_html_safe() -> None:
    card = confirmation_card(
        {
            "kind": "event",
            "title": "Созвон <важный> & срочный",
            "start_at_utc": "2026-08-04T12:00:00Z",
            "due_at_utc": None,
            "duration_min": 90,
            "rrule": None,
            "tz": "Europe/Moscow",
            "local_time": "15:00:00",
            "reminders_min_before": [60, 15],
        }
    )

    assert "Создать событие?" in card
    assert "04.08.2026" in card
    assert "15:00 (Europe/Moscow)" in card
    assert "Созвон &lt;важный&gt; &amp; срочный" in card
    assert "1 ч 30 мин" in card


def test_ai_preview_can_be_edited_as_manual_fields() -> None:
    manual = manual_fields_from_normalized(
        {
            "kind": "routine",
            "title": "Английский",
            "start_at_utc": "2026-08-04T09:00:00+00:00",
            "due_at_utc": None,
            "duration_min": 60,
            "rrule": "FREQ=WEEKLY;BYDAY=MO,WE,FR",
            "tz": "Europe/Moscow",
            "reminders_min_before": [],
        }
    )

    assert manual["local_time"] == "12:00"
    assert manual["recurrence"]["byweekday"] == ["mon", "wed", "fri"]


def test_callbacks_are_bounded_and_nonce_is_checked() -> None:
    nonce = "123e4567-e89b-12d3-a456-426614174000"
    markups = [confirmation_keyboard(nonce), edit_keyboard(nonce, routine=True)]
    callback_data = [
        button.callback_data
        for markup in markups
        for row in markup.inline_keyboard
        for button in row
    ]
    assert all(value is not None and len(value.encode()) <= 64 for value in callback_data)
    assert _nonce_matches(f"confirm:create:{nonce}", {"nonce": nonce})
    assert not _nonce_matches("confirm:create:stale", {"nonce": nonce})


def test_manual_syntax_is_deterministic_and_bounded() -> None:
    assert _parse_recurrence("пн, ср, пт")["byweekday"] == ["mon", "wed", "fri"]
    assert _parse_reminders("1440, 60, 60") == [1440, 60]
    assert _parse_reminders("нет") == [0]
    with pytest.raises(ValueError):
        _parse_reminders("1,2,3,4,5,6")


def test_dispatcher_serializes_events_per_user() -> None:
    dependencies = _dependencies(FakeTextService())
    dispatcher = build_dispatcher(_settings(), dependencies)

    assert isinstance(dispatcher.fsm.events_isolation, SimpleEventIsolation)
    assert any(
        isinstance(middleware, ActiveReactionMiddleware)
        for middleware in dispatcher.message.outer_middleware._middlewares
    )
    assert any(
        isinstance(middleware, ActiveReactionMiddleware)
        for middleware in dispatcher.callback_query.outer_middleware._middlewares
    )
    names = [router.name for router in dispatcher.sub_routers]
    assert "occurrence-actions" in names
    assert names.index("my-day") < names.index("entry-creation")
    assert names.index("occurrence-actions") < names.index("entry-creation")


def test_main_menu_exposes_my_day_without_callback_payload() -> None:
    menu = main_menu()
    assert menu.keyboard[0][0].text == "Мой день"


@pytest.mark.parametrize("entry_kind", list(EntryKind))
def test_notification_reaction_keyboard_is_bounded(entry_kind: EntryKind) -> None:
    command = DeliveryCommand(
        notification_id=2**63 - 1,
        occurrence_id=2,
        user_id=3,
        telegram_id=4,
        title="Рутина",
        entry_kind=entry_kind,
        notification_kind=NotificationKind.MAIN,
        planned_at_utc=NOW,
        fire_at_utc=NOW,
        silent=False,
        late=False,
    )
    keyboard = notification_keyboard(command)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert labels == ["Выполнено", "Через час", "Перенести", "Пропустить"]
    assert callbacks == [
        f"n:done:{2**63 - 1}",
        f"n:snooze:{2**63 - 1}",
        f"n:move:{2**63 - 1}",
        f"n:skip:{2**63 - 1}",
    ]
    assert all(value is not None and len(value.encode()) <= 64 for value in callbacks)


def test_move_keyboard_has_stable_target_and_back_action() -> None:
    keyboard = move_keyboard(
        2**63 - 1,
        target_minute=31_903_200,
        target_label="Завтра 06.08 15:00",
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert callbacks == [
        f"n:at:31903200:{2**63 - 1}",
        f"n:back:{2**63 - 1}",
    ]
    assert all(value is not None and len(value.encode()) <= 64 for value in callbacks)


def test_reaction_middleware_reads_message_and_callback_actor() -> None:
    event = cast(object, SimpleNamespace(from_user=SimpleNamespace(id=123456789)))
    assert event_actor_id(event) == 123456789  # type: ignore[arg-type]


def test_reply_lookup_is_limited_to_owner_private_chat() -> None:
    private_reply = cast(
        object,
        SimpleNamespace(
            reply_to_message=SimpleNamespace(message_id=321),
            chat=SimpleNamespace(id=123456789, type="private"),
        ),
    )
    group_reply = cast(
        object,
        SimpleNamespace(
            reply_to_message=SimpleNamespace(message_id=321),
            chat=SimpleNamespace(id=-100123, type="supergroup"),
        ),
    )
    plain = cast(object, SimpleNamespace(reply_to_message=None))

    assert reply_to_message_id(private_reply, owner_id=123456789) == 321  # type: ignore[arg-type]
    assert reply_to_message_id(group_reply, owner_id=123456789) == 0  # type: ignore[arg-type]
    assert reply_to_message_id(plain, owner_id=123456789) is None  # type: ignore[arg-type]


async def test_reminder_reply_filter_requires_resolved_context() -> None:
    filter_ = ResolvedReactionContext()
    message = cast(Message, object())

    assert not await filter_(message)
    assert not await filter_(message, ActiveReactionResult(reacted=False))
    assert await filter_(message, ActiveReactionResult(reacted=True, occurrence_id=1))


async def test_unknown_reply_preserves_manual_date_flow() -> None:
    service = FakeTextService()
    router = build_router(_settings().telegram_owner_id, _dependencies(service))
    state = _fsm_context()
    await state.set_state(ManualForm.date)
    message = AsyncMock(spec=Message)
    message.answer = AsyncMock()
    message.text = "05.08.2026"
    message.reply_to_message = SimpleNamespace(message_id=999)

    await router.message.trigger(
        message,
        bot=AsyncMock(),
        raw_state=ManualForm.date.state,
        state=state,
    )

    assert await state.get_state() == ManualForm.time.state
    assert service.calls == 0
    message.answer.assert_awaited_once()


async def test_unknown_reply_without_state_does_not_call_nim() -> None:
    service = FakeTextService()
    router = build_router(_settings().telegram_owner_id, _dependencies(service))
    state = _fsm_context()
    message = AsyncMock(spec=Message)
    message.answer = AsyncMock()
    message.text = "обычный ответ"
    message.reply_to_message = SimpleNamespace(message_id=999)

    await router.message.trigger(message, bot=AsyncMock(), raw_state=None, state=state)

    assert service.calls == 0
    message.answer.assert_awaited_once_with("Не нашёл напоминание, на которое ты ответил.")


def _fsm_context() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=2, user_id=3),
    )


@pytest.mark.parametrize(
    ("kind", "expected_state"),
    [
        ("event", ManualForm.duration),
        ("task", ManualForm.duration),
        ("routine", ManualForm.recurrence),
    ],
)
async def test_fsm_branches_after_time(kind: str, expected_state: object) -> None:
    state = _fsm_context()
    await state.set_data({"kind": kind})
    message = AsyncMock()

    await _prompt_after_time(message, state)

    assert await state.get_state() == expected_state.state
    message.answer.assert_awaited_once()


async def test_validated_payload_enters_confirmation_with_nonce() -> None:
    state = _fsm_context()
    message = AsyncMock()
    payload = {
        "kind": "event",
        "title": "Созвон",
        "start_at_utc": "2026-08-04T12:00:00Z",
        "due_at_utc": None,
        "duration_min": None,
        "rrule": None,
        "tz": "Europe/Moscow",
        "local_time": "15:00:00",
        "reminders_min_before": [],
    }

    await _store_and_show(message, state, payload)

    data = await state.get_data()
    assert await state.get_state() == Confirmation.ready.state
    assert data["normalized"] == payload
    assert data["nonce"]
    message.answer.assert_awaited_once()
