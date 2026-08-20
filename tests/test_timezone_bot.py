"""Telegram UX смены таймзоны не содержит бизнес-логики и AI."""

from __future__ import annotations

import datetime as dt
from typing import cast
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from seshat.bot import build_dependencies, build_dispatcher
from seshat.config import Settings
from seshat.domain.timezones import TimezoneChangePreview, TimezoneReviewItem
from seshat.telegram.timezones import (
    TimezoneFlow,
    _choose_keyboard,
    _confirmation_keyboard,
    _preview_text,
    _review_keyboard,
    _review_text,
    _show_review,
)


class _UnusedTextService:
    def __init__(self) -> None:
        self.calls = 0

    async def prepare_text(self, *_: object, **__: object) -> object:
        self.calls += 1
        raise AssertionError("timezone flow must not call AI")


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        telegram_bot_token="123456789:" + "AAtest-token-value-for-tests-only-xxxx",
        telegram_owner_id=123456789,
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        env="test",
    )


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=2, user_id=3),
    )


def _review_item() -> TimezoneReviewItem:
    return TimezoneReviewItem(
        entry_id=42,
        kind="event",
        title="Интервью <важное>",
        moment_field="start_at_utc",
        moment_at_utc=dt.datetime(2030, 8, 5, 12, tzinfo=dt.UTC),
        keep_absolute_local=dt.datetime(2030, 8, 5, 14, tzinfo=dt.timezone(dt.timedelta(hours=2))),
        keep_local_at_utc=dt.datetime(2030, 8, 5, 13, tzinfo=dt.UTC),
        keep_local_local=dt.datetime(2030, 8, 5, 15, tzinfo=dt.timezone(dt.timedelta(hours=2))),
    )


def test_timezone_router_precedes_generic_text_and_does_not_construct_ai_call() -> None:
    service = _UnusedTextService()
    dependencies = build_dependencies(
        _settings(),
        cast(async_sessionmaker[AsyncSession], object()),
        cast(object, service),
    )

    dispatcher = build_dispatcher(_settings(), dependencies)

    assert dispatcher.sub_routers[0].name == "timezone-change"
    assert dispatcher.sub_routers[1].name == "occurrence-actions"
    assert service.calls == 0


def test_timezone_callbacks_fit_telegram_limit() -> None:
    markups = [
        _choose_keyboard(),
        _confirmation_keyboard("3fddcb17-1ef5-4494-b45c-9c615c1d874b"),
        _review_keyboard(2**63 - 1),
    ]
    callbacks = [
        button.callback_data
        for markup in markups
        for row in markup.inline_keyboard
        for button in row
    ]
    assert all(value is not None and len(value.encode()) <= 64 for value in callbacks)


def test_timezone_cards_are_explicit_and_html_safe() -> None:
    preview = TimezoneChangePreview(
        confirmation_id="3fddcb17-1ef5-4494-b45c-9c615c1d874b",
        tz_from="Europe/Moscow",
        tz_to="Europe/Amsterdam",
        now_from=dt.datetime(2026, 8, 3, 15, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        now_to=dt.datetime(2026, 8, 3, 14, tzinfo=dt.timezone(dt.timedelta(hours=2))),
        routine_count=2,
        review_count=1,
    )

    preview_card = _preview_text(preview)
    review_card = _review_text(_review_item())

    assert "15:00 → 14:00" in preview_card
    assert "Рутин будет пересчитано: 2" in preview_card
    assert "Оставить момент — будет 05.08.2030 14:00" in review_card
    assert "Оставить местное время — будет 05.08.2030 15:00" in review_card
    assert "Интервью &lt;важное&gt;" in review_card


async def test_show_review_persists_only_json_ids_in_fsm() -> None:
    state = _state()
    message = AsyncMock()

    await _show_review(message, state, 7, _review_item())

    assert await state.get_state() == TimezoneFlow.reviewing.state
    assert await state.get_data() == {"tz_change_id": 7, "tz_entry_id": 42}
    message.answer.assert_awaited_once()
