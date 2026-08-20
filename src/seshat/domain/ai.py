"""Единый orchestration-путь подготовки записи из свободного текста.

API и Telegram получают типизированный результат и не решают самостоятельно,
когда переспрашивать пользователя или переключать его на ручную форму.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from seshat.config import Settings
from seshat.db.enums import AiOutcome
from seshat.db.models import AiCall
from seshat.domain.nim import (
    AttemptTelemetry,
    NimAttempt,
    NimClient,
    NimError,
    NimResponseError,
    NimResult,
)
from seshat.domain.nim_ops import NimGate, NimParseCache, PostgresNimGate
from seshat.domain.parsing import NeedsClarification, NormalizedEntry, ParseError, normalize


class PreparationSource(StrEnum):
    AI = "ai"
    CACHE = "cache"


class ManualFallbackReason(StrEnum):
    AI_UNAVAILABLE = "ai_unavailable"
    TELEMETRY_UNAVAILABLE = "telemetry_unavailable"


@dataclass(frozen=True, slots=True)
class TextReady:
    entry: NormalizedEntry
    source: PreparationSource
    nim: NimResult


@dataclass(frozen=True, slots=True)
class TextClarification:
    reason: str


@dataclass(frozen=True, slots=True)
class TextManualFallback:
    reason: ManualFallbackReason


TextPreparation = TextReady | TextClarification | TextManualFallback
NimClientFactory = Callable[[AttemptTelemetry], NimClient]


class TextPreparationService:
    """NIM + cache + gate + журнал попыток как одна доменная операция.

    ``user_id`` должен принадлежать уже сохранённому пользователю: телеметрия
    намеренно коммитится отдельной короткой сессией и не зависит от последующего
    commit/rollback draft записи.
    """

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        engine: AsyncEngine | None = None,
        gate: NimGate | None = None,
        cache: NimParseCache | None = None,
        client_factory: NimClientFactory | None = None,
    ) -> None:
        if engine is not None and gate is not None:
            raise ValueError("передайте либо engine, либо готовый NIM gate")
        self._settings = settings
        self._session_factory = session_factory
        self._gate = PostgresNimGate(engine) if engine is not None else gate
        self._cache = cache or NimParseCache()
        self._client_factory = client_factory

    async def prepare_text(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        text: str,
        tz: str,
        now_utc: dt.datetime,
    ) -> TextPreparation:
        """Возвращает карточку-кандидат, переспрос или переход в ручную форму."""

        # Сессия — часть стабильного domain-интерфейса и гарантирует, что адаптер
        # уже работает в пользовательском DB-контексте. AI telemetry пишет через
        # отдельную factory, чтобы её не откатил отказ от будущего draft.
        if session.in_transaction() and session.new:
            await session.flush()

        successful_call_id: int | None = None

        async def record_attempt(attempt: NimAttempt) -> None:
            nonlocal successful_call_id
            call_id = await self._write_attempt(user_id, attempt)
            if attempt.outcome.value in {AiOutcome.OK.value, AiOutcome.FALLBACK.value}:
                successful_call_id = call_id

        client = self._make_client(record_attempt)
        try:
            async with client:
                result = await client.parse(text, tz=tz, now_utc=now_utc)
        except ValueError as exc:
            return TextClarification(str(exc))
        except NimResponseError:
            return TextClarification("ответ модели не прошёл структурную проверку")
        except NimError:
            return TextManualFallback(ManualFallbackReason.AI_UNAVAILABLE)
        except SQLAlchemyError:
            # Без межпроцессной координации/журнала не делаем скрытый AI-вызов;
            # ручная форма остаётся полностью работоспособной без NVIDIA.
            return TextManualFallback(ManualFallbackReason.TELEMETRY_UNAVAILABLE)

        try:
            entry = normalize(result.plan, tz=tz, now_utc=now_utc)
        except (NeedsClarification, ParseError) as exc:
            if successful_call_id is not None:
                try:
                    await self._mark_clarification(successful_call_id)
                except SQLAlchemyError:
                    return TextManualFallback(ManualFallbackReason.TELEMETRY_UNAVAILABLE)
            return TextClarification(str(exc))

        return TextReady(
            entry=entry,
            source=PreparationSource.CACHE if result.cache_hit else PreparationSource.AI,
            nim=result,
        )

    def _make_client(self, telemetry: AttemptTelemetry) -> NimClient:
        if self._client_factory is not None:
            return self._client_factory(telemetry)
        return NimClient(
            self._settings,
            gate=self._gate,
            cache=self._cache,
            telemetry=telemetry,
        )

    async def _write_attempt(self, user_id: int, attempt: NimAttempt) -> int:
        async with self._session_factory() as session:
            row = AiCall(
                user_id=user_id,
                model=attempt.model,
                latency_ms=attempt.latency_ms,
                prompt_tokens=attempt.prompt_tokens,
                completion_tokens=attempt.completion_tokens,
                outcome=AiOutcome(attempt.outcome.value),
                needed_clarification=attempt.needed_clarification,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def _mark_clarification(self, ai_call_id: int) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(AiCall).where(AiCall.id == ai_call_id).values(needed_clarification=True)
            )
            await session.commit()
