"""Фоновый runtime долговечного планировщика.

В памяти живут только два периодических цикла. Само расписание и состояние
доставки всегда читаются из PostgreSQL, поэтому перезапуск процесса безопасен.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from seshat.config import Settings
from seshat.db.base import session_scope
from seshat.domain.delivery import (
    DeliveryTickResult,
    NotificationTransport,
    RepeatPolicy,
    RetryPolicy,
    deliver_due,
)
from seshat.domain.digests import (
    DigestTickResult,
    MorningDigestTransport,
    deliver_due_morning_digests,
)
from seshat.domain.scheduling import (
    MaterializeResult,
    ReminderDefaults,
    ScheduleResult,
    materialize_occurrences,
    schedule_notifications,
)

log = logging.getLogger(__name__)


class SchedulerTransport(NotificationTransport, MorningDigestTransport, Protocol):
    pass


class SchedulerRuntime:
    """Запускает reconciliation раз в час и delivery tick по конфигу."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        transport: SchedulerTransport,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        reconcile_interval_s: int = 3600,
    ) -> None:
        if reconcile_interval_s < 1:
            raise ValueError("reconcile_interval_s must be positive")
        self._settings = settings
        self._session_factory = session_factory
        self._transport = transport
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._reconcile_interval_s = reconcile_interval_s

    async def reconcile(
        self, *, now_utc: dt.datetime | None = None
    ) -> tuple[MaterializeResult, ScheduleResult]:
        """Достраивает occurrence/notification строки; повторный вызов безопасен."""
        now = now_utc or self._clock()
        defaults = ReminderDefaults(
            event_pre_min=self._settings.default_event_reminders_min,
            task_pre_min=self._settings.default_task_reminders_min,
            task_morning_local=self._settings.default_task_morning_local,
            routine_pre_min=self._settings.default_routine_reminders_min,
        )
        async with session_scope(self._session_factory) as session:
            materialized = await materialize_occurrences(
                session,
                now_utc=now,
                horizon_days=self._settings.materialize_horizon_days,
                lookback_minutes=self._settings.late_delivery_threshold_min,
            )
            # Планируем все pending occurrences, а не только что созданные:
            # это автоматически чинит незавершённый предыдущий reconciliation.
            scheduled = await schedule_notifications(
                session,
                now_utc=now,
                defaults=defaults,
            )
        log.info(
            "расписание сверено",
            extra={
                "extra_fields": {
                    "occurrences_created": len(materialized.created_occurrence_ids),
                    "notifications_created": len(scheduled.created_notification_ids),
                    "notifications_reactivated": len(scheduled.reactivated_notification_ids),
                }
            },
        )
        return materialized, scheduled

    async def tick(self, *, now_utc: dt.datetime | None = None) -> DeliveryTickResult:
        """Доставляет одну ограниченную пачку уже сохранённых уведомлений."""
        result = await deliver_due(
            self._session_factory,
            self._transport,
            now_utc=now_utc or self._clock(),
            late_threshold_min=self._settings.late_delivery_threshold_min,
            batch_size=self._settings.delivery_batch_size,
            retry_policy=RetryPolicy(
                base_delay_s=self._settings.delivery_retry_base_s,
                max_delay_s=self._settings.delivery_retry_max_s,
                max_attempts=self._settings.delivery_max_attempts,
            ),
            repeat_policy=RepeatPolicy(
                interval_min=self._settings.important_repeat_interval_min,
                max_repeats=self._settings.important_repeat_max,
            ),
            active_context_ttl_min=self._settings.active_context_ttl_min,
        )
        if any(
            (
                result.sent,
                result.missed,
                result.retried,
                result.failed,
                result.cancelled,
                result.rescheduled,
            )
        ):
            log.info(
                "tick уведомлений завершён",
                extra={
                    "extra_fields": {
                        "sent": result.sent,
                        "missed": result.missed,
                        "retried": result.retried,
                        "failed": result.failed,
                        "cancelled": result.cancelled,
                        "rescheduled": result.rescheduled,
                    }
                },
            )
        return result

    async def run(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Работает до отмены задачи или установки ``stop_event``."""
        stop = stop_event or asyncio.Event()
        async with asyncio.TaskGroup() as group:
            group.create_task(self._reconcile_loop(stop))
            group.create_task(self._tick_loop(stop))

    async def digest_tick(self, *, now_utc: dt.datetime | None = None) -> DigestTickResult:
        """Доставляет due-дайджесты по текущей пользовательской таймзоне."""
        result = await deliver_due_morning_digests(
            self._session_factory,
            self._transport,
            now_utc=now_utc or self._clock(),
            batch_size=self._settings.delivery_batch_size,
        )
        if any((result.sent, result.empty, result.retried, result.failed)):
            log.info(
                "tick утреннего дайджеста завершён",
                extra={
                    "extra_fields": {
                        "sent": result.sent,
                        "empty": result.empty,
                        "retried": result.retried,
                        "failed": result.failed,
                    }
                },
            )
        return result

    async def _reconcile_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка reconciliation планировщика")
            await _wait_or_stop(stop, self._reconcile_interval_s)

    async def _tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
                await self.digest_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка tick-лупа уведомлений")
            await _wait_or_stop(stop, self._settings.tick_interval_s)


async def _wait_or_stop(stop: asyncio.Event, timeout_s: int) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout_s)


__all__ = ["SchedulerRuntime"]
