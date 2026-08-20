"""Health и readiness.

Разделены намеренно: `/health` отвечает «процесс жив» и не ходит в БД,
`/ready` подтверждает, что БД доступна и миграции применены. Монитор,
который дёргает только `/health`, покажет зелёный при мёртвой базе.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from seshat import __version__
from seshat.api.deps import SessionDep, SettingsDep
from seshat.api.schemas import HealthOut, ReadinessOut

router = APIRouter(tags=["service"])


@router.get("/health", response_model=HealthOut, summary="Процесс жив")
async def health(settings: SettingsDep) -> HealthOut:
    return HealthOut(version=__version__, env=settings.env)


@router.get("/ready", response_model=ReadinessOut, summary="Готов обслуживать запросы")
async def ready(session: SessionDep) -> ReadinessOut:
    await session.execute(text("SELECT 1"))

    # Наличие таблицы проверяем отдельно: в базе, поднятой не миграциями
    # (например, в тестах), её нет — и это не повод отвечать пятисоткой.
    has_table = (
        await session.execute(text("SELECT to_regclass('public.alembic_version')"))
    ).scalar_one_or_none()
    revision = None
    if has_table is not None:
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()

    return ReadinessOut(status="ok", database="ok", migration=revision)
