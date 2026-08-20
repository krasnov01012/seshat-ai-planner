"""Сборка FastAPI-приложения."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from seshat import __version__
from seshat.api.routers import day, entries, health, occurrences
from seshat.api.routers import settings as settings_router
from seshat.config import Settings, load_settings
from seshat.db.base import make_engine, make_session_factory
from seshat.domain import DomainError
from seshat.domain.ai import TextPreparationService

log = logging.getLogger(__name__)

DESCRIPTION = """
Персональный диспетчер: события, задачи и рутины, напоминания и разбор дня.

HTTP-интерфейс — полноценный, а не довесок к боту: вся логика живёт в слое
домена, а Telegram-бот и это API одинаково являются его клиентами.
"""


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(config.database_url)
        session_factory = make_session_factory(engine)
        app.state.settings = config
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.text_preparation_service = TextPreparationService(
            config,
            session_factory,
            engine=engine,
        )
        log.info("api запущен", extra={"extra_fields": {"env": config.env}})
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Seshat API",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        # Compose публикует API только на loopback. При внешнем развёртывании
        # доступ к документации должен ограничивать отдельный ingress.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        """Нарушение правила предметной области — это 400, а не 500."""
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    # CORS остаётся выключенным, пока список origins пуст: включать «на всякий случай»
    # с `*` нельзя — это открыло бы API любому сайту в браузере пользователя.
    if config.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(health.router)
    app.include_router(settings_router.router)
    app.include_router(entries.router)
    app.include_router(occurrences.router)
    app.include_router(day.router)
    return app
