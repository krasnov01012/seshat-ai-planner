"""Зависимости FastAPI: сессия БД и аутентификация."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from seshat.config import Settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос: commit при успехе, rollback при исключении."""
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_settings_obj(request: Request) -> Settings:
    return request.app.state.settings


async def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer-токен.

    Сравнение постоянное по времени: обычное `==` на строках завершается
    на первом несовпавшем символе и по времени ответа выдаёт префикс токена.
    """
    expected = request.app.state.settings.api_token.get_secret_value()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_TOKEN не задан на сервере",
        )

    scheme, _, provided = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="нужен заголовок Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_obj)]
AuthDep = Depends(require_token)
