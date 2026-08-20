"""Transparent Telegram hook: any owner reaction stops important repeats."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from seshat.db.base import session_scope
from seshat.domain.delivery import react_to_message_context
from seshat.domain.users import find_user_by_telegram_id
from seshat.telegram.contracts import TelegramDependencies

log = logging.getLogger(__name__)


class ActiveReactionMiddleware(BaseMiddleware):
    def __init__(self, owner_id: int, dependencies: TelegramDependencies) -> None:
        self._owner_id = owner_id
        self._dependencies = dependencies

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        actor_id = event_actor_id(event)
        if actor_id == self._owner_id and not _is_explicit_reaction(event):
            try:
                async with session_scope(self._dependencies.session_factory) as session:
                    user = await find_user_by_telegram_id(session, actor_id)
                    if user is not None:
                        reply_message_id = reply_to_message_id(event, owner_id=self._owner_id)
                        reaction = await react_to_message_context(
                            session,
                            user.id,
                            reply_telegram_message_id=reply_message_id,
                            reacted_at_utc=self._dependencies.clock(),
                        )
                        if reaction.reacted or reply_message_id is not None:
                            data["reaction_context"] = reaction
            except Exception:
                log.exception("не удалось остановить повторы по реакции владельца")
        return await handler(event, data)


def event_actor_id(event: TelegramObject) -> int | None:
    actor = getattr(event, "from_user", None) or getattr(event, "user", None)
    return getattr(actor, "id", None)


def reply_to_message_id(event: TelegramObject, *, owner_id: int | None = None) -> int | None:
    """Return 0 for an explicit but unsafe/unusable reply, preventing active fallback."""
    reply = getattr(event, "reply_to_message", None)
    if reply is None:
        return None
    if owner_id is not None:
        chat = getattr(event, "chat", None)
        if getattr(chat, "type", None) != "private" or getattr(chat, "id", None) != owner_id:
            return 0
    message_id = getattr(reply, "message_id", None)
    return message_id if isinstance(message_id, int) and message_id > 0 else 0


def _is_explicit_reaction(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        command = (event.text or "").strip().split(maxsplit=1)[0].casefold().split("@", 1)[0]
        return command in {"/ack", "/skip"}
    if isinstance(event, CallbackQuery):
        return bool(event.data and event.data.startswith(("notify:", "n:")))
    return False


__all__ = ["ActiveReactionMiddleware", "event_actor_id", "reply_to_message_id"]
