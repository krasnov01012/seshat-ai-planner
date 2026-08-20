"""Операционные примитивы для надёжного доступа к NVIDIA NIM.

Модуль не знает ни про Telegram, ни про HTTP API.  Здесь находятся только
межпроцессная координация через PostgreSQL и небольшой эфемерный кэш разбора.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import unicodedata
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from seshat.domain.parsing import ParsedPlan

# Один и тот же ключ обязаны использовать api и bot. Число фиксировано, а не
# зависит от hash(), который рандомизируется между процессами Python.
DEFAULT_NIM_ADVISORY_LOCK_KEY = 7_329_479_831_944_556_973


class NimGate(Protocol):
    """Межпроцессный ограничитель одной полной NIM-цепочки."""

    def hold(self, *, timeout_s: float) -> AbstractAsyncContextManager[None]: ...


class PostgresNimGate:
    """Session-level advisory lock, общий для отдельных api/bot процессов.

    Соединение удерживается до конца retry/fallback-цепочки. Неудачный unlock
    инвалидирует соединение: обычный rollback не снимает session-level lock и
    иначе заблокированная сессия могла бы вернуться в pool.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        lock_key: int = DEFAULT_NIM_ADVISORY_LOCK_KEY,
        poll_interval_s: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("интервал опроса advisory lock должен быть положительным")
        self._engine = engine
        self._lock_key = lock_key
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._sleep = sleep

    @asynccontextmanager
    async def hold(self, *, timeout_s: float) -> AsyncIterator[None]:
        if timeout_s <= 0:
            raise TimeoutError("deadline межпроцессного NIM gate исчерпан")

        deadline = self._clock() + timeout_s
        connection: AsyncConnection | None = None
        acquired = False
        try:
            connection = await asyncio.wait_for(
                self._engine.connect(), timeout=_positive_remaining(deadline, self._clock)
            )
            while True:
                acquired = bool(
                    await asyncio.wait_for(
                        connection.scalar(
                            text("SELECT pg_try_advisory_lock(:lock_key)"),
                            {"lock_key": self._lock_key},
                        ),
                        timeout=_positive_remaining(deadline, self._clock),
                    )
                )
                # SELECT начинает implicit transaction. Session lock переживает
                # commit, зато соединение не остаётся idle in transaction.
                await connection.commit()
                if acquired:
                    yield
                    return
                await asyncio.wait_for(
                    self._sleep(
                        min(
                            self._poll_interval_s,
                            _positive_remaining(deadline, self._clock),
                        )
                    ),
                    timeout=_positive_remaining(deadline, self._clock),
                )
        except TimeoutError:
            if acquired:
                raise
            raise TimeoutError("deadline межпроцессного NIM gate исчерпан") from None
        finally:
            if connection is not None:
                try:
                    if acquired:
                        await self._unlock_or_invalidate(connection)
                finally:
                    await connection.close()

    async def _unlock_or_invalidate(self, connection: AsyncConnection) -> None:
        try:
            unlocked = bool(
                await connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self._lock_key},
                )
            )
            await connection.commit()
            if not unlocked:
                raise RuntimeError("PostgreSQL не подтвердил освобождение NIM lock")
        except BaseException:
            # Не возвращаем потенциально залоченную server session в pool.
            await connection.invalidate()
            raise


class NimParseCache:
    """Bounded process-local TTL/LRU cache только для валидированных ответов."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("размер NIM cache должен быть положительным")
        if ttl_s <= 0:
            raise ValueError("TTL NIM cache должен быть положительным")
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> ParsedPlan | None:
        async with self._lock:
            cached = self._entries.get(key)
            if cached is None:
                return None
            expires_at, payload = cached
            if expires_at <= self._clock():
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            # Новый объект не позволяет вызывающему коду изменить кэш.
            return ParsedPlan.model_validate_json(payload, strict=True)

    async def put(self, key: str, plan: ParsedPlan) -> None:
        payload = plan.model_dump_json()
        async with self._lock:
            self._purge_expired()
            self._entries[key] = (self._clock() + self._ttl_s, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def _purge_expired(self) -> None:
        now = self._clock()
        expired = [key for key, (expires_at, _) in self._entries.items() if expires_at <= now]
        for key in expired:
            del self._entries[key]


def normalize_nim_input(value: str) -> str:
    """Канонизирует Unicode и пробелы, не меняя регистр пользовательских имён."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def make_nim_cache_key(
    *,
    text_value: str,
    context: str | None,
    system_prompt: str,
    models: list[str],
    response_schema: dict[str, object],
) -> str:
    """SHA-256 полного детерминирующего входа, без raw-текста в ключе."""

    canonical = json.dumps(
        {
            "contract": 1,
            "text": normalize_nim_input(text_value),
            "context": None if context is None else normalize_nim_input(context),
            "system_prompt": system_prompt,
            "models": models,
            "response_schema": response_schema,
            "temperature": 0,
            "max_tokens": 1200,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _positive_remaining(deadline: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError
    return remaining
