"""Сборка Telegram-приложения и тонкие адаптеры доменного слоя."""

from __future__ import annotations

import datetime as dt
import uuid

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from seshat.config import Settings
from seshat.db.enums import EntryKind
from seshat.domain.ai import (
    TextClarification,
    TextManualFallback,
    TextPreparationService,
    TextReady,
)
from seshat.domain.entries import (
    ManualEntryInput as DomainManualEntryInput,
)
from seshat.domain.entries import (
    prepare_manual_entry,
)
from seshat.domain.parsing import (
    NormalizedEntry,
    Recurrence,
)
from seshat.domain.planning import create_planned_entry
from seshat.domain.scheduling import ReminderDefaults
from seshat.telegram.contracts import (
    Clarification,
    ManualEntryInput,
    ManualFallback,
    Ready,
    TelegramDependencies,
)
from seshat.telegram.day import build_day_router
from seshat.telegram.occurrences import build_occurrence_router
from seshat.telegram.reactions import ActiveReactionMiddleware
from seshat.telegram.router import build_router
from seshat.telegram.timezones import build_timezone_router


def build_dependencies(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    text_service: TextPreparationService,
) -> TelegramDependencies:
    async def prepare_text(
        session: AsyncSession,
        user_id: int,
        text: str,
        *,
        tz: str,
        now_utc: dt.datetime,
    ) -> Ready | Clarification | ManualFallback:
        result = await text_service.prepare_text(
            session,
            user_id=user_id,
            text=text,
            tz=tz,
            now_utc=now_utc,
        )
        if isinstance(result, TextReady):
            return Ready(result.entry.model_dump(mode="json"))
        if isinstance(result, TextClarification):
            return Clarification()
        if isinstance(result, TextManualFallback):
            return ManualFallback()
        raise TypeError("TextPreparationService вернул неизвестный результат")

    def prepare_manual(manual: ManualEntryInput, tz: str, now_utc: dt.datetime) -> NormalizedEntry:
        local = dt.datetime.combine(manual.local_date, manual.local_time)
        recurrence = Recurrence.model_validate(manual.recurrence) if manual.recurrence else None
        kind = EntryKind(manual.kind)
        domain_input = DomainManualEntryInput(
            kind=kind,
            title=manual.title,
            start=local if kind in {EntryKind.EVENT, EntryKind.ROUTINE} else None,
            due=local if kind is EntryKind.TASK else None,
            duration_min=manual.duration_min,
            recurrence=recurrence,
            reminders_min_before=list(manual.reminders_min_before),
        )
        return prepare_manual_entry(domain_input, tz=tz, now_utc=now_utc)

    async def persist(
        session: AsyncSession,
        user_id: int,
        normalized: dict[str, object],
        confirmation_id: str,
    ) -> object:
        draft = NormalizedEntry.model_validate(normalized)
        return await create_planned_entry(
            session,
            user_id,
            draft,
            confirmation_id=uuid.UUID(confirmation_id),
            now_utc=dt.datetime.now(dt.UTC),
            horizon_days=settings.materialize_horizon_days,
            late_lookback_minutes=settings.late_delivery_threshold_min,
            defaults=ReminderDefaults(
                event_pre_min=settings.default_event_reminders_min,
                task_pre_min=settings.default_task_reminders_min,
                task_morning_local=settings.default_task_morning_local,
                routine_pre_min=settings.default_routine_reminders_min,
            ),
        )

    return TelegramDependencies(
        session_factory=session_factory,
        prepare_text=prepare_text,
        prepare_manual=prepare_manual,
        create_entry=persist,
        default_tz=settings.default_tz,
    )


def build_dispatcher(settings: Settings, dependencies: TelegramDependencies) -> Dispatcher:
    dp = Dispatcher(events_isolation=SimpleEventIsolation())
    reaction_middleware = ActiveReactionMiddleware(settings.telegram_owner_id, dependencies)
    dp.message.outer_middleware(reaction_middleware)
    dp.callback_query.outer_middleware(reaction_middleware)
    # До общего text-handler создания: «Настройки» и /timezone не должны уйти в AI.
    dp.include_router(build_timezone_router(settings.telegram_owner_id, dependencies, settings))
    dp.include_router(build_occurrence_router(settings.telegram_owner_id, dependencies, settings))
    dp.include_router(build_day_router(settings.telegram_owner_id, dependencies))
    dp.include_router(build_router(settings.telegram_owner_id, dependencies))
    return dp


def build_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
