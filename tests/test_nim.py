"""NVIDIA NIM: schema, retry/fallback, semaphore и fail-closed разбор."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import traceback
from collections.abc import Awaitable

import httpx
import pytest

from seshat.config import Settings
from seshat.domain.nim import (
    NimClient,
    NimProviderError,
    NimResponseError,
    NimUnavailable,
    build_system_prompt,
    parsed_plan_json_schema,
)
from seshat.domain.parsing import Intent

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


def ok_response(model: str = "primary") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": plan_json()}}],
            "usage": {"prompt_tokens": 101, "completion_tokens": 42},
            "model": model,
        },
    )


def test_prompt_contains_local_date_weekday_and_timezone() -> None:
    prompt = build_system_prompt(now_utc=NOW, tz="Europe/Moscow")
    assert "2026-08-03" in prompt
    assert "понедельник" in prompt
    assert "Europe/Moscow" in prompt
    assert "не определяй тип записи" in prompt
    assert "текущую локальную дату" in prompt
    assert "сегодняшнее время уже прошло" in prompt


def test_prompt_uses_local_date_after_timezone_conversion() -> None:
    before_midnight_utc = dt.datetime(2026, 8, 3, 22, 30, tzinfo=dt.UTC)
    prompt = build_system_prompt(now_utc=before_midnight_utc, tz="Europe/Moscow")
    assert "2026-08-04" in prompt
    assert "вторник" in prompt


def test_prompt_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        build_system_prompt(now_utc=dt.datetime(2026, 8, 3, 7, 0), tz="Europe/Moscow")


def test_schema_does_not_ask_model_for_kind() -> None:
    schema = parsed_plan_json_schema()
    assert "kind" not in schema["properties"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    start_schema = schema["properties"]["start"]["anyOf"][0]
    assert "format" not in start_schema
    assert start_schema["pattern"].startswith("^")


@pytest.mark.asyncio
async def test_valid_response_is_parsed_and_usage_returned() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        result = await NimClient(settings(), http=http).parse(
            "Завтра в 15:00 собеседование", tz="Europe/Moscow", now_utc=NOW
        )

    assert result.plan.intent is Intent.CREATE
    assert result.plan.title == "Собеседование"
    assert result.model == "primary"
    assert result.attempts == 1
    assert result.used_fallback is False
    assert result.prompt_tokens == 101
    assert result.completion_tokens == 42
    body = json.loads(seen[0].content)
    assert body["response_format"]["type"] == "json_schema"
    assert body["model"] == "primary"
    assert seen[0].headers["Authorization"].startswith("Bearer nvapi-")


@pytest.mark.asyncio
async def test_primary_retries_three_times_then_uses_fallback() -> None:
    models: list[str] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "primary":
            return httpx.Response(429)
        return ok_response(model)

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        result = await NimClient(settings(), http=http, sleep=no_sleep).parse(
            "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
        )

    assert models == ["primary", "primary", "primary", "primary", "fallback"]
    assert delays == [1, 2, 4]
    assert result.model == "fallback"
    assert result.attempts == 5
    assert result.used_fallback is True


@pytest.mark.asyncio
async def test_second_fallback_is_used_after_retryable_failure() -> None:
    models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model in {"primary", "fallback"}:
            return httpx.Response(503)
        return ok_response(model)

    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        result = await NimClient(settings(), http=http, sleep=no_sleep).parse(
            "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
        )

    assert models == ["primary", "primary", "primary", "primary", "fallback", "fallback-2"]
    assert result.model == "fallback-2"


@pytest.mark.asyncio
async def test_all_temporary_failures_raise_unavailable() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def no_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimUnavailable, match="все модели"):
            await NimClient(settings(), http=http, sleep=no_sleep).parse(
                "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
            )


@pytest.mark.asyncio
async def test_non_retryable_http_error_fails_without_fallback() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimProviderError, match="HTTP 400"):
            await NimClient(settings(), http=http).parse(
                "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
            )
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_json_fails_closed() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimResponseError, match="валидацию"):
            await NimClient(settings(), http=http).parse(
                "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
            )


@pytest.mark.asyncio
async def test_validation_traceback_does_not_contain_private_model_output() -> None:
    private = "PRIVATE_PLAN_SHOULD_NOT_REACH_LOGS"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": plan_json(title=private, confidence=1.5)}}]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimResponseError) as caught:
            await NimClient(settings(), http=http).parse(
                "Личный план", tz="Europe/Moscow", now_utc=NOW
            )

    rendered = "".join(traceback.format_exception(caught.value))
    assert private not in rendered


@pytest.mark.asyncio
async def test_datetime_with_offset_fails_closed() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": plan_json(start="2026-08-04T15:00Z")}}]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimResponseError):
            await NimClient(settings(), http=http).parse(
                "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
            )


@pytest.mark.asyncio
async def test_semaphore_serializes_concurrent_requests() -> None:
    active = 0
    max_active = 0
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        if not first_entered.is_set():
            first_entered.set()
            await release_first.wait()
        active -= 1
        return ok_response()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        first_client = NimClient(settings(), http=http)
        second_client = NimClient(settings(), http=http)
        first = asyncio.create_task(
            first_client.parse("Первая фраза", tz="Europe/Moscow", now_utc=NOW)
        )
        await first_entered.wait()
        second = asyncio.create_task(
            second_client.parse("Вторая фраза", tz="Europe/Moscow", now_utc=NOW)
        )
        await asyncio.sleep(0)
        assert max_active == 1
        release_first.set()
        await asyncio.gather(first, second)

    assert max_active == 1


@pytest.mark.asyncio
async def test_network_timeout_moves_to_fallback_without_three_more_timeouts() -> None:
    models: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "primary":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return ok_response(model)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        result = await NimClient(settings(), http=http).parse(
            "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
        )

    assert models == ["primary", "fallback"]
    assert result.model == "fallback"


@pytest.mark.asyncio
async def test_fast_transport_error_retries_primary() -> None:
    calls = 0
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("synthetic connect error", request=request)
        return ok_response()

    async def no_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        result = await NimClient(settings(), http=http, sleep=no_sleep).parse(
            "Завтра собеседование", tz="Europe/Moscow", now_utc=NOW
        )

    assert calls == 3
    assert delays == [1, 2]
    assert result.model == "primary"


@pytest.mark.asyncio
async def test_text_and_context_are_bounded_before_network() -> None:
    async with NimClient(settings()) as client:
        with pytest.raises(ValueError, match="длиннее"):
            await client.parse("x" * 4097, tz="Europe/Moscow", now_utc=NOW)
        with pytest.raises(ValueError, match="контекст"):
            await client.parse("коротко", tz="Europe/Moscow", now_utc=NOW, context="x" * 4097)


@pytest.mark.asyncio
async def test_total_deadline_includes_retry_backoff() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "10"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://nim.test/v1"
    ) as http:
        with pytest.raises(NimUnavailable, match="общий deadline"):
            await NimClient(
                settings(nvidia_total_timeout_s=0.01), http=http, sleep=asyncio.sleep
            ).parse("Завтра собеседование", tz="Europe/Moscow", now_utc=NOW)


def test_empty_text_is_rejected_before_network() -> None:
    async def never(_: float) -> Awaitable[None]:
        raise AssertionError("sleep must not be called")

    # Constructor itself does not touch the network; the coroutine rejects
    # before the first HTTP request.
    async def run() -> None:
        async with NimClient(settings(), sleep=never) as client:
            with pytest.raises(ValueError, match="пустым"):
                await client.parse("   ", tz="Europe/Moscow", now_utc=NOW)

    asyncio.run(run())
