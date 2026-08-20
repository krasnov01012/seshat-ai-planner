"""Клиент NVIDIA NIM для структурированного разбора планов.

AI здесь только извлекает поля. Решения о типе записи, допустимости дат и
сохранении принимает :mod:`seshat.domain.parsing` и последующие доменные
сервисы. Сырой ответ провайдера никогда не выходит из этого модуля без
Pydantic-валидации.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import ValidationError

from seshat.config import Settings
from seshat.domain.nim_ops import NimGate, NimParseCache, make_nim_cache_key, normalize_nim_input
from seshat.domain.parsing import CONFIDENCE_THRESHOLD, ParsedPlan

_WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

# Лимит общий для всех клиентов в одном процессе. API и bot пока являются
# разными процессами/контейнерами; межпроцессный лимит потребует внешнего
# координатора и не заявляется как выполненный этим семафором.
_PROCESS_SEMAPHORE = asyncio.Semaphore(1)
_PROCESS_SEMAPHORES: dict[int, asyncio.Semaphore] = {1: _PROCESS_SEMAPHORE}

MAX_NIM_TEXT_CHARS = 4096
MAX_NIM_CONTEXT_CHARS = 4096


class NimError(Exception):
    """Базовая ошибка обращения к NIM."""


class NimUnavailable(NimError):
    """Все разрешённые модели временно недоступны."""


class NimProviderError(NimError):
    """Провайдер вернул ошибку, которую нельзя безопасно повторять."""


class NimResponseError(NimError):
    """Ответ провайдера не соответствует ожидаемой схеме."""


@dataclass(frozen=True, slots=True)
class NimResult:
    """Проверенный результат и метаданные для будущего журнала ``ai_calls``."""

    plan: ParsedPlan
    model: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    attempts: int
    used_fallback: bool
    cache_hit: bool = False
    attempt_records: tuple[NimAttempt, ...] = ()


class NimAttemptOutcome(StrEnum):
    """Исход одной фактической HTTP-попытки, совместимый с ``ai_calls``."""

    OK = "ok"
    RETRY = "retry"
    FALLBACK = "fallback"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NimAttempt:
    """Безопасная телеметрия попытки: без текста, URL, headers и response body."""

    attempt: int
    model: str
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    outcome: NimAttemptOutcome
    needed_clarification: bool


AttemptTelemetry = Callable[[NimAttempt], Awaitable[None]]


def build_system_prompt(*, now_utc: dt.datetime, tz: str) -> str:
    """Строит prompt с текущей локальной датой, днём недели и таймзоной."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc должен быть tz-aware")

    local = now_utc.astimezone(ZoneInfo(tz))
    weekday = _WEEKDAYS_RU[local.weekday()]
    return (
        "Ты извлекаешь структурированные поля из русской фразы для персонального "
        "планировщика. "
        f"Сейчас {local:%Y-%m-%d}, {weekday}; таймзона пользователя: {tz}. "
        "Не возвращай поле kind и не определяй тип записи: его выведет код. "
        "Все даты и время возвращай как местное время пользователя без UTC-смещения. "
        "Для разового события укажи start; для задачи только с дедлайном укажи due "
        "и оставь start=null. Для повторяющегося действия обязательно укажи recurrence, "
        "а start поставь на текущую локальную дату с названным временем как якорь, "
        "даже если сегодняшнее время уже прошло; только из-за этого не проси уточнение. "
        "Явные слова повторения обязательны: «каждый день»/«ежедневно» означают "
        "recurrence.freq=daily; перечисление дней недели означает freq=weekly и "
        "byweekday с этими днями. Не оставляй recurrence=null при таких маркерах. "
        "recurrence=null означает отсутствие повторения. snooze_min и target_ref "
        "используй только для соответствующих не-create намерений, иначе верни null. "
        "confidence — число от 0 до 1, отражающее уверенность во всём разборе. "
        "Если дата, время или намерение неоднозначны, установи "
        "needs_clarification=true и не угадывай. "
        "Поля, которых нет во фразе, возвращай как null; "
        "reminders_min_before возвращай массивом."
    )


def parsed_plan_json_schema() -> dict[str, Any]:
    """Строгая JSON Schema транспортного ответа NIM.

    Все поля обязательны на транспортном уровне, но смысловые optional-поля
    принимают ``null``. Так модель не может молча опустить поле, а Pydantic
    остаётся вторым независимым рубежом проверки.
    """
    nullable_string = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    # Не используем JSON Schema `format: date-time`: RFC 3339 требует
    # offset, а доменный контракт — local wall-clock без offset.
    local_datetime = {
        "type": "string",
        "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$",
    }
    nullable_datetime = {"anyOf": [local_datetime, {"type": "null"}]}
    nullable_integer = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    recurrence = {
        "anyOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "freq": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
                    "byweekday": {
                        "anyOf": [
                            {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                                },
                            },
                            {"type": "null"},
                        ]
                    },
                    "interval": {"type": "integer", "minimum": 1},
                },
                "required": ["freq", "byweekday", "interval"],
            },
            {"type": "null"},
        ]
    }
    properties: dict[str, Any] = {
        "intent": {
            "type": "string",
            "enum": ["create", "reschedule", "snooze", "complete", "skip", "unknown"],
        },
        "title": nullable_string,
        "start": nullable_datetime,
        "due": nullable_datetime,
        "duration_min": nullable_integer,
        "recurrence": recurrence,
        "reminders_min_before": {"type": "array", "items": {"type": "integer"}},
        "snooze_min": nullable_integer,
        "target_ref": nullable_string,
        "needs_clarification": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


class NimClient:
    """Асинхронный клиент NIM с retry, fallback и process-wide semaphore."""

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        semaphore: asyncio.Semaphore | None = None,
        gate: NimGate | None = None,
        cache: NimParseCache | None = None,
        telemetry: AttemptTelemetry | None = None,
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._semaphore = semaphore or _process_semaphore(settings.nvidia_max_concurrency)
        self._gate = gate
        self._cache = cache
        self._telemetry = telemetry
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=settings.nvidia_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.nvidia_timeout_s),
        )

    async def __aenter__(self) -> NimClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def parse(
        self,
        text: str,
        *,
        tz: str,
        now_utc: dt.datetime,
        context: str | None = None,
    ) -> NimResult:
        """Разбирает фразу и возвращает только прошедший схему результат."""
        cleaned_text = normalize_nim_input(text)
        if not cleaned_text:
            raise ValueError("текст для разбора не может быть пустым")
        if len(cleaned_text) > MAX_NIM_TEXT_CHARS:
            raise ValueError(f"текст для разбора длиннее {MAX_NIM_TEXT_CHARS} символов")
        if context is not None and len(context) > MAX_NIM_CONTEXT_CHARS:
            raise ValueError(f"контекст длиннее {MAX_NIM_CONTEXT_CHARS} символов")

        cleaned_context = None if context is None else normalize_nim_input(context)
        system_prompt = build_system_prompt(now_utc=now_utc, tz=tz)
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": cleaned_text
                if cleaned_context is None
                else (
                    f"Контекст активного напоминания: {cleaned_context}\nСообщение: {cleaned_text}"
                ),
            },
        ]
        models = list(
            dict.fromkeys(
                (
                    self._settings.nvidia_model_primary,
                    self._settings.nvidia_model_fallback,
                    self._settings.nvidia_model_fallback_2,
                )
            )
        )

        response_schema = parsed_plan_json_schema()
        cache_key = make_nim_cache_key(
            text_value=cleaned_text,
            context=cleaned_context,
            system_prompt=system_prompt,
            models=models,
            response_schema=response_schema,
        )
        cached = await self._cached_result(cache_key)
        if cached is not None:
            return cached

        attempts = 0
        attempt_records: list[NimAttempt] = []
        last_temporary_error: str | None = None
        deadline = time.monotonic() + self._settings.nvidia_total_timeout_s
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=min(self._settings.nvidia_timeout_s, _remaining(deadline)),
            )
        except TimeoutError:
            raise NimUnavailable("общий deadline NIM исчерпан в очереди") from None

        try:
            gate_entered = False
            if self._gate is not None:
                try:
                    gate_context = self._gate.hold(timeout_s=_remaining(deadline))
                    await gate_context.__aenter__()
                    gate_entered = True
                except TimeoutError:
                    raise NimUnavailable(
                        "общий deadline NIM исчерпан в межпроцессной очереди"
                    ) from None
            else:
                gate_context = None

            started = time.perf_counter()
            cached = await self._cached_result(cache_key)
            if cached is not None:
                return cached
            for model_index, model in enumerate(models):
                # Документированный порядок: три retry у основной модели,
                # затем по одной попытке запасных. Это ограничивает худшее
                # время ожидания и не умножает timeout на 12 запросов.
                max_attempts = 1 + self._settings.nvidia_max_retries if model_index == 0 else 1
                for attempt_index in range(max_attempts):
                    if _remaining(deadline) <= 0:
                        raise NimUnavailable("общий deadline NIM исчерпан")
                    attempts += 1
                    attempt_started = time.perf_counter()
                    try:
                        response = await asyncio.wait_for(
                            self._http.post(
                                "/chat/completions",
                                headers={
                                    "Authorization": (
                                        "Bearer " + self._settings.nvidia_api_key.get_secret_value()
                                    ),
                                    "Accept": "application/json",
                                },
                                json={
                                    "model": model,
                                    "messages": messages,
                                    "temperature": 0,
                                    "max_tokens": 1200,
                                    "response_format": {
                                        "type": "json_schema",
                                        "json_schema": {
                                            "name": "parsed_plan",
                                            "strict": True,
                                            "schema": response_schema,
                                        },
                                    },
                                },
                            ),
                            timeout=min(self._settings.nvidia_timeout_s, _remaining(deadline)),
                        )
                    except (TimeoutError, httpx.TimeoutException) as exc:
                        # Сетевой timeout уже израсходовал до 25 секунд;
                        # повторять его ещё трижды слишком дорого. Сразу берём
                        # следующую модель, не раскрывая URL/headers в ошибке.
                        last_temporary_error = type(exc).__name__
                        await self._record_attempt(
                            attempt_records,
                            attempt=attempts,
                            model=model,
                            started=attempt_started,
                            outcome=(
                                NimAttemptOutcome.RETRY
                                if model_index + 1 < len(models)
                                else NimAttemptOutcome.FAILED
                            ),
                        )
                        break
                    except httpx.TransportError as exc:
                        # Быстрый connect/reset/protocol error можно повторить;
                        # в отличие от timeout он не израсходовал всё окно.
                        last_temporary_error = type(exc).__name__
                        if attempt_index + 1 < max_attempts:
                            attempt_latency_ms = _elapsed_ms(attempt_started)
                            try:
                                await self._sleep_before_retry(2**attempt_index, deadline)
                            except NimUnavailable:
                                await self._record_attempt(
                                    attempt_records,
                                    attempt=attempts,
                                    model=model,
                                    started=attempt_started,
                                    latency_ms=attempt_latency_ms,
                                    outcome=NimAttemptOutcome.FAILED,
                                )
                                raise
                            await self._record_attempt(
                                attempt_records,
                                attempt=attempts,
                                model=model,
                                started=attempt_started,
                                latency_ms=attempt_latency_ms,
                                outcome=NimAttemptOutcome.RETRY,
                            )
                            continue
                        await self._record_attempt(
                            attempt_records,
                            attempt=attempts,
                            model=model,
                            started=attempt_started,
                            outcome=(
                                NimAttemptOutcome.RETRY
                                if model_index + 1 < len(models)
                                else NimAttemptOutcome.FAILED
                            ),
                        )
                        break

                    if response.status_code == 429 or response.status_code >= 500:
                        last_temporary_error = f"HTTP {response.status_code}"
                        if attempt_index + 1 < max_attempts:
                            attempt_latency_ms = _elapsed_ms(attempt_started)
                            try:
                                await self._sleep_before_retry(
                                    _retry_delay(response, attempt_index), deadline
                                )
                            except NimUnavailable:
                                await self._record_attempt(
                                    attempt_records,
                                    attempt=attempts,
                                    model=model,
                                    started=attempt_started,
                                    latency_ms=attempt_latency_ms,
                                    outcome=NimAttemptOutcome.FAILED,
                                )
                                raise
                            await self._record_attempt(
                                attempt_records,
                                attempt=attempts,
                                model=model,
                                started=attempt_started,
                                latency_ms=attempt_latency_ms,
                                outcome=NimAttemptOutcome.RETRY,
                            )
                            continue
                        await self._record_attempt(
                            attempt_records,
                            attempt=attempts,
                            model=model,
                            started=attempt_started,
                            outcome=(
                                NimAttemptOutcome.RETRY
                                if model_index + 1 < len(models)
                                else NimAttemptOutcome.FAILED
                            ),
                        )
                        break

                    # Модель могла быть снята с публикации, при этом запасная
                    # остаётся рабочей. Ошибки credentials/request так не маскируем.
                    if response.status_code == 404:
                        last_temporary_error = "HTTP 404 model unavailable"
                        await self._record_attempt(
                            attempt_records,
                            attempt=attempts,
                            model=model,
                            started=attempt_started,
                            outcome=(
                                NimAttemptOutcome.RETRY
                                if model_index + 1 < len(models)
                                else NimAttemptOutcome.FAILED
                            ),
                        )
                        break

                    if response.is_error:
                        await self._record_attempt(
                            attempt_records,
                            attempt=attempts,
                            model=model,
                            started=attempt_started,
                            outcome=NimAttemptOutcome.FAILED,
                        )
                        raise NimProviderError(
                            f"NIM вернул неповторяемую ошибку HTTP {response.status_code}"
                        )

                    try:
                        plan, usage = self._validated_payload(response)
                    except NimResponseError:
                        await self._record_attempt(
                            attempt_records,
                            attempt=attempts,
                            model=model,
                            started=attempt_started,
                            outcome=NimAttemptOutcome.FAILED,
                        )
                        raise
                    outcome = (
                        NimAttemptOutcome.FALLBACK if model_index > 0 else NimAttemptOutcome.OK
                    )
                    await self._record_attempt(
                        attempt_records,
                        attempt=attempts,
                        model=model,
                        started=attempt_started,
                        outcome=outcome,
                        prompt_tokens=_optional_int(usage.get("prompt_tokens")),
                        completion_tokens=_optional_int(usage.get("completion_tokens")),
                        needed_clarification=(
                            plan.needs_clarification or plan.confidence < CONFIDENCE_THRESHOLD
                        ),
                    )
                    if (
                        self._cache is not None
                        and not plan.needs_clarification
                        and plan.confidence >= CONFIDENCE_THRESHOLD
                    ):
                        await self._cache.put(cache_key, plan)
                    latency_ms = round((time.perf_counter() - started) * 1000)
                    return NimResult(
                        plan=plan,
                        model=model,
                        latency_ms=latency_ms,
                        prompt_tokens=_optional_int(usage.get("prompt_tokens")),
                        completion_tokens=_optional_int(usage.get("completion_tokens")),
                        attempts=attempts,
                        used_fallback=model_index > 0,
                        attempt_records=tuple(attempt_records),
                    )

        finally:
            try:
                if gate_entered and gate_context is not None:
                    await gate_context.__aexit__(None, None, None)
            finally:
                self._semaphore.release()

        suffix = f" ({last_temporary_error})" if last_temporary_error else ""
        raise NimUnavailable(f"все модели NIM временно недоступны{suffix}")

    async def _cached_result(self, cache_key: str) -> NimResult | None:
        if self._cache is None:
            return None
        plan = await self._cache.get(cache_key)
        if plan is None:
            return None
        return NimResult(
            plan=plan,
            model="cache",
            latency_ms=0,
            prompt_tokens=None,
            completion_tokens=None,
            attempts=0,
            used_fallback=False,
            cache_hit=True,
        )

    async def _record_attempt(
        self,
        records: list[NimAttempt],
        *,
        attempt: int,
        model: str,
        started: float,
        outcome: NimAttemptOutcome,
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        needed_clarification: bool = False,
    ) -> None:
        record = NimAttempt(
            attempt=attempt,
            model=model,
            latency_ms=_elapsed_ms(started) if latency_ms is None else latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            outcome=outcome,
            needed_clarification=needed_clarification,
        )
        records.append(record)
        if self._telemetry is not None:
            await self._telemetry(record)

    async def _sleep_before_retry(self, delay: float, deadline: float) -> None:
        try:
            await asyncio.wait_for(self._sleep(delay), timeout=_remaining(deadline))
        except TimeoutError:
            raise NimUnavailable("общий deadline NIM исчерпан во время backoff") from None

    @staticmethod
    def _validated_payload(response: httpx.Response) -> tuple[ParsedPlan, dict[str, Any]]:
        try:
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content должен быть строкой")
            plan = ParsedPlan.model_validate_json(content, strict=True)
            usage = envelope.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
        except (ValueError, TypeError, KeyError, IndexError, ValidationError):
            # Не chain-им исходное исключение: ValidationError включает raw
            # input и может вынести личный текст плана в traceback/VPS logs.
            raise NimResponseError("ответ NIM не прошёл структурную валидацию") from None
        return plan, usage


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _process_semaphore(limit: int) -> asyncio.Semaphore:
    """Возвращает общий semaphore для всех клиентов текущего процесса."""
    if limit < 1:
        raise ValueError("лимит параллельности NIM должен быть положительным")
    semaphore = _PROCESS_SEMAPHORES.get(limit)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _PROCESS_SEMAPHORES[limit] = semaphore
    return semaphore


def _retry_delay(response: httpx.Response, attempt_index: int) -> float:
    """Уважает короткий Retry-After, иначе использует 1/2/4 секунды."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), 10.0)
        except ValueError:
            pass
    return float(2**attempt_index)


def _remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)
