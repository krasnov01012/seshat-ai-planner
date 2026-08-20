"""Точка входа: `python -m seshat`."""

from __future__ import annotations

import asyncio
import logging

from seshat.bot import build_bot, build_dependencies, build_dispatcher
from seshat.config import load_settings
from seshat.db.base import make_engine, make_session_factory
from seshat.domain.ai import TextPreparationService
from seshat.logging_setup import setup_logging
from seshat.scheduler import SchedulerRuntime
from seshat.telegram.delivery import AiogramNotificationTransport

log = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    log.info(
        "старт",
        extra={
            "extra_fields": {
                "env": settings.env,
                "model": settings.nvidia_model_primary,
                "tz": settings.default_tz,
            }
        },
    )

    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    text_service = TextPreparationService(settings, session_factory, engine=engine)
    dependencies = build_dependencies(settings, session_factory, text_service)
    bot = build_bot(settings)
    dp = build_dispatcher(settings, dependencies)

    me = await bot.get_me()
    log.info("бот подключён", extra={"extra_fields": {"username": me.username}})

    scheduler_stop = asyncio.Event()
    scheduler = SchedulerRuntime(
        settings,
        session_factory,
        AiogramNotificationTransport(bot),
    )
    scheduler_task = asyncio.create_task(
        scheduler.run(stop_event=scheduler_stop),
        name="seshat-scheduler",
    )
    try:
        # Накопленные реакции нельзя отбрасывать: /ack или /skip, отправленный
        # во время рестарта, должен остановить повторы после восстановления.
        await dp.start_polling(bot, drop_pending_updates=False)
    finally:
        scheduler_stop.set()
        try:
            await asyncio.wait_for(
                scheduler_task,
                timeout=settings.scheduler_shutdown_timeout_s,
            )
        except TimeoutError:
            log.warning("планировщик не остановился вовремя, задача отменена")
            scheduler_task.cancel()
            await asyncio.gather(scheduler_task, return_exceptions=True)
        await engine.dispose()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("остановка")
