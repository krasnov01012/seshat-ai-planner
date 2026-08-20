"""Кэш, телеметрия, PostgreSQL gate и общий orchestration AI-разбора."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import fields

import httpx
import pytest
from sqlalchemy import select

from seshat.config import Settings
from seshat.db.base import make_engine, make_session_factory, session_scope
from seshat.db.enums import AiOutcome, EntryKind
from seshat.db.models import AiCall, User, UserSettings
from seshat.domain.ai import (
    ManualFallbackReason,
    PreparationSource,
    TextClarification,
    TextManualFallback,
    TextPreparationService,
    TextReady,
)
from seshat.domain.entries import ManualEntryInput, create_entry, prepare_manual_entry
from seshat.domain.nim import (
    NimAttempt,
    NimAttemptOutcome,
    NimClient,
    NimProviderError,
    NimUnavailable,
)
from seshat.domain.nim_ops import (
    NimParseCache,
    PostgresNimGate,
    make_nim_cache_key,
)
from seshat.domain.parsing import Intent, ParsedPlan

NOW = dt.datetime(2026, 8, 3, 7, 0, tzinfo=dt.UTC)


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "telegram_bot_token": "123456789:" + "AAtest-token-value-for-tests-only-xxxx",
        "telegram_owner_id": 42,
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "nvidia_api_key": "nvapi-" + "test-value-never-sent-to-network",
        "nvidia_model_primary": "primary",
        "nvidia_model_fallback": "fallback",
        "nvidia_model_fallback_2": "fallback-2",
        "nvidia_max_retries": 3,
    }
    return Settings(**{**values, **overrides})  # type: ignore[arg-type]


def plan_json(**overrides: object) -> str:
    plan: dict[str, object] = {
        "intent": "create",
        "title": "Собеседование",
        "start": "2026-08-04T15:00:00",
        "due": None,
        "duration_min": None,
        "recurrence": None,
        "reminders_min_before": [60],
        "snooze_min": None,
        "target_ref": None,
        "needs_clarification": False,
        "confidence": 0.95,
    }
    return json.dumps({**plan, **overrides}, ensure_ascii=False)


def ok_response(**plan_overrides: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": plan_json(**plan_overrides)}}],
            "usage": {"prompt_tokens": 101, "completion_tokens": 42},
        },
    )


class SerialGate:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, *, timeout_s: float):
        await asyncio.wait_for(self.lock.acquire(), timeout=timeout_s)
        try:
            yield
        finally:
            self.lock.release()


class TimeoutGate:
    @asynccontextmanager
    async def hold(self, *, timeout_s: float):
        del timeout_s
        raise TimeoutError
        yield  # pragma: no cover - делает функцию async generator


@pytest.mark.asyncio
async def test_cache_hit_skips_transport_and_telemetry() -> None:
    calls = 0
    attempts: list[NimAttempt] = []
    cache = NimParseCache()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok_response()

    async def telemetry(attempt: NimAttempt) -> None:
        attempts.append(attempt)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        client = NimClient(settings(), http=http, cache=cache, telemetry=telemetry)
        first = await client.parse(
            "  Завтра\u00a0в 15:00  собеседование ", tz="Europe/Moscow", now_utc=NOW
        )
        second = await client.parse("Завтра в 15:00 собеседование", tz="Europe/Moscow", now_utc=NOW)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.attempts == 0
    assert calls == 1
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_cache_double_check_prevents_same_process_stampede() -> None:
    calls = 0
    cache = NimParseCache()
    gate = SerialGate()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return ok_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        first = NimClient(
            settings(), http=http, cache=cache, gate=gate, semaphore=asyncio.Semaphore(1)
        )
        second = NimClient(
            settings(), http=http, cache=cache, gate=gate, semaphore=asyncio.Semaphore(1)
        )
        results = await asyncio.gather(
            first.parse("Завтра собеседование", tz="Europe/Moscow", now_utc=NOW),
            second.parse("Завтра собеседование", tz="Europe/Moscow", now_utc=NOW),
        )

    assert calls == 1
    assert sorted(result.cache_hit for result in results) == [False, True]


@pytest.mark.asyncio
async def test_ambiguous_result_is_not_cached() -> None:
    calls = 0
    cache = NimParseCache()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok_response(needs_clarification=True, confidence=0.4)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        client = NimClient(settings(), http=http, cache=cache)
        first = await client.parse("В следующую пятницу", tz="Europe/Moscow", now_utc=NOW)
        second = await client.parse("В следующую пятницу", tz="Europe/Moscow", now_utc=NOW)

    assert first.cache_hit is False
    assert second.cache_hit is False
    assert calls == 2


@pytest.mark.asyncio
async def test_cache_is_ttl_bounded_lru_and_returns_copy() -> None:
    now = [10.0]
    cache = NimParseCache(max_entries=2, ttl_s=5, clock=lambda: now[0])
    plan = ParsedPlan(intent=Intent.CREATE, title="A", confidence=0.9)

    await cache.put("a", plan)
    await cache.put("b", plan.model_copy(update={"title": "B"}))
    loaded = await cache.get("a")
    assert loaded is not None
    loaded.title = "mutated"

    await cache.put("c", plan.model_copy(update={"title": "C"}))
    assert await cache.get("b") is None
    assert (await cache.get("a")).title == "A"  # type: ignore[union-attr]

    now[0] = 16.0
    assert await cache.get("a") is None
    assert await cache.get("c") is None


def test_cache_key_covers_context_prompt_models_and_schema() -> None:
    base = {
        "text_value": "  План\u00a0на завтра ",
        "context": None,
        "system_prompt": "date=2026-08-03 tz=Europe/Moscow",
        "models": ["primary", "fallback"],
        "response_schema": {"type": "object"},
    }
    first = make_nim_cache_key(**base)
    assert first == make_nim_cache_key(**{**base, "text_value": "План на завтра"})
    assert first != make_nim_cache_key(**{**base, "context": "другая запись"})
    assert first != make_nim_cache_key(**{**base, "system_prompt": "date=2026-08-04"})
    assert first != make_nim_cache_key(**{**base, "models": ["other"]})
    assert first != make_nim_cache_key(**{**base, "response_schema": {"type": "string"}})


@pytest.mark.asyncio
async def test_all_attempts_have_sanitized_retry_fallback_telemetry() -> None:
    recorded: list[NimAttempt] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(429) if model == "primary" else ok_response()

    async def no_sleep(_: float) -> None:
        return None

    async def telemetry(attempt: NimAttempt) -> None:
        recorded.append(attempt)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        result = await NimClient(settings(), http=http, sleep=no_sleep, telemetry=telemetry).parse(
            "PRIVATE TEXT", tz="Europe/Moscow", now_utc=NOW
        )

    assert [item.model for item in recorded] == [
        "primary",
        "primary",
        "primary",
        "primary",
        "fallback",
    ]
    assert [item.outcome for item in recorded] == [
        NimAttemptOutcome.RETRY,
        NimAttemptOutcome.RETRY,
        NimAttemptOutcome.RETRY,
        NimAttemptOutcome.RETRY,
        NimAttemptOutcome.FALLBACK,
    ]
    assert recorded[-1].prompt_tokens == 101
    assert result.attempt_records == tuple(recorded)
    assert "PRIVATE TEXT" not in repr(recorded)
    assert {field.name for field in fields(NimAttempt)} == {
        "attempt",
        "model",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "outcome",
        "needed_clarification",
    }


@pytest.mark.asyncio
async def test_terminal_failure_is_emitted_and_gate_timeout_skips_network() -> None:
    recorded: list[NimAttempt] = []

    async def telemetry(attempt: NimAttempt) -> None:
        recorded.append(attempt)

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="PRIVATE PROVIDER BODY")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimProviderError, match="HTTP 400"):
            await NimClient(settings(), http=http, telemetry=telemetry).parse(
                "PRIVATE USER TEXT", tz="Europe/Moscow", now_utc=NOW
            )

    assert [item.outcome for item in recorded] == [NimAttemptOutcome.FAILED]
    assert "PRIVATE" not in repr(recorded)

    calls = 0

    async def never(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(never), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimUnavailable, match="межпроцессной очереди"):
            await NimClient(settings(), http=http, gate=TimeoutGate()).parse(
                "План", tz="Europe/Moscow", now_utc=NOW
            )
    assert calls == 0


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — AI telemetry не проверяется",
)
@pytest.mark.asyncio
async def test_invalid_structured_response_requests_clarification(session, session_factory) -> None:
    user = User(telegram_id=901_003)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id))
    await session.flush()
    config = settings()

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        service = TextPreparationService(
            config,
            session_factory,
            client_factory=lambda telemetry: NimClient(
                config, http=http, telemetry=telemetry, semaphore=asyncio.Semaphore(1)
            ),
        )
        result = await service.prepare_text(
            session,
            user_id=user.id,
            text="Личный текст",
            tz="Europe/Moscow",
            now_utc=NOW,
        )

    assert isinstance(result, TextClarification)
    row = (await session.execute(select(AiCall).where(AiCall.user_id == user.id))).scalar_one()
    assert row.outcome is AiOutcome.FAILED


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — PostgreSQL gate не проверяется",
)
@pytest.mark.asyncio
async def test_postgres_gate_serializes_connections_and_releases_after_timeout(engine) -> None:
    second_engine = make_engine(os.environ["TEST_DATABASE_URL"])
    lock_key = time.time_ns() % 8_000_000_000_000_000_000
    first_gate = PostgresNimGate(engine, lock_key=lock_key, poll_interval_s=0.01)
    second_gate = PostgresNimGate(second_engine, lock_key=lock_key, poll_interval_s=0.01)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_first() -> None:
        async with first_gate.hold(timeout_s=1):
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold_first())
    await entered.wait()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            async with second_gate.hold(timeout_s=0.05):
                raise AssertionError("два процесса вошли в NIM critical section")
    finally:
        release.set()
        await task

    # Если session lock утёк в pool, повторное получение здесь зависнет.
    async with second_gate.hold(timeout_s=0.5):
        pass
    await second_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — AI telemetry не проверяется",
)
@pytest.mark.asyncio
async def test_preparation_service_writes_ai_call_and_returns_ready(
    session, session_factory
) -> None:
    user = User(telegram_id=901_001)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id))
    await session.flush()
    config = settings()

    async def handler(_: httpx.Request) -> httpx.Response:
        return ok_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        cache = NimParseCache()
        service = TextPreparationService(
            config,
            session_factory,
            cache=cache,
            client_factory=lambda telemetry: NimClient(
                config,
                http=http,
                cache=cache,
                telemetry=telemetry,
                semaphore=asyncio.Semaphore(1),
            ),
        )
        prepared = await service.prepare_text(
            session,
            user_id=user.id,
            text="Завтра в 15:00 собеседование",
            tz="Europe/Moscow",
            now_utc=NOW,
        )
        cached = await service.prepare_text(
            session,
            user_id=user.id,
            text="Завтра в 15:00 собеседование",
            tz="Europe/Moscow",
            now_utc=NOW,
        )

    assert isinstance(prepared, TextReady)
    assert prepared.source is PreparationSource.AI
    assert isinstance(cached, TextReady)
    assert cached.source is PreparationSource.CACHE
    rows = (await session.execute(select(AiCall).where(AiCall.user_id == user.id))).scalars().all()
    assert len(rows) == 1
    assert rows[0].outcome is AiOutcome.OK
    assert rows[0].prompt_tokens == 101


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — AI telemetry не проверяется",
)
@pytest.mark.asyncio
async def test_preparation_service_marks_clarification_and_manual_fallback(
    session, session_factory
) -> None:
    user = User(telegram_id=901_002)
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id))
    await session.flush()
    config = settings()
    # Модель сама не просит уточнение, но бизнес-валидация отвергает далёкое
    # прошлое. Service обязан обновить уже записанную успешную AI-попытку.
    responses = [ok_response(start="2020-01-01T15:00:00"), httpx.Response(401)]

    async def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        service = TextPreparationService(
            config,
            session_factory,
            client_factory=lambda telemetry: NimClient(
                config, http=http, telemetry=telemetry, semaphore=asyncio.Semaphore(1)
            ),
        )
        clarification = await service.prepare_text(
            session, user_id=user.id, text="Когда-нибудь", tz="Europe/Moscow", now_utc=NOW
        )
        fallback = await service.prepare_text(
            session, user_id=user.id, text="Завтра встреча", tz="Europe/Moscow", now_utc=NOW
        )

    assert isinstance(clarification, TextClarification)
    assert isinstance(fallback, TextManualFallback)
    assert fallback.reason is ManualFallbackReason.AI_UNAVAILABLE
    rows = (
        (await session.execute(select(AiCall).where(AiCall.user_id == user.id).order_by(AiCall.id)))
        .scalars()
        .all()
    )
    assert [row.outcome for row in rows] == [AiOutcome.OK, AiOutcome.FAILED]
    assert rows[0].needed_clarification is True


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — broken-key fallback не проверяется",
)
@pytest.mark.asyncio
async def test_broken_key_falls_back_to_manual_and_creates_without_more_ai(
    engine, committed_user_ids
) -> None:
    factory = make_session_factory(engine)
    telegram_id = uuid.uuid4().int % 9_000_000_000 + 20_000_000_000
    async with session_scope(factory) as setup_session:
        user = User(telegram_id=telegram_id)
        setup_session.add(user)
        await setup_session.flush()
        setup_session.add(UserSettings(user_id=user.id, tz="Europe/Moscow"))
        user_id = user.id
        committed_user_ids.append(user_id)

    calls = 0

    async def broken_key(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    config = settings(nvidia_api_key="definitely-broken")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(broken_key), base_url="https://nim.test/v1"
    ) as http:
        service = TextPreparationService(
            config,
            factory,
            client_factory=lambda telemetry: NimClient(
                config, http=http, telemetry=telemetry, semaphore=asyncio.Semaphore(1)
            ),
        )
        async with session_scope(factory) as ai_session:
            preparation = await service.prepare_text(
                ai_session,
                user_id=user_id,
                text="Завтра в 15:00 собеседование",
                tz="Europe/Moscow",
                now_utc=NOW,
            )

        assert isinstance(preparation, TextManualFallback)
        assert calls == 1

        draft = prepare_manual_entry(
            ManualEntryInput(
                kind=EntryKind.EVENT,
                title="Собеседование через ручную форму",
                start=dt.datetime(2026, 8, 4, 15, 0),
            ),
            tz="Europe/Moscow",
            now_utc=NOW,
        )
        async with session_scope(factory) as create_session:
            created = await create_entry(
                create_session,
                user_id,
                draft,
                confirmation_id=uuid.uuid4(),
            )

    assert created.created is True
    assert calls == 1
