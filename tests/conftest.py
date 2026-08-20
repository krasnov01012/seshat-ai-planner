"""Фикстуры для тестов, которым нужна настоящая PostgreSQL.

Схему проверяем на реальной базе, а не на SQLite: ключевые гарантии проекта —
частичный индекс, `timestamptz`, `ARRAY`, `JSONB` и составные UNIQUE — в SQLite
либо ведут себя иначе, либо не существуют. Тест на «почти такой же» базе
не доказывает ничего.

Без `TEST_DATABASE_URL` интеграционные тесты пропускаются, чтобы юнит-тесты
оставались запускаемыми без Docker.

Изоляция сделана внешней транзакцией, а не очисткой таблиц. Вариант с `TRUNCATE`
в teardown приводил к вечному зависанию: очистка ждёт ACCESS EXCLUSIVE, а сессия
теста в этот момент ещё держит свою транзакцию. Здесь же тест работает внутри
транзакции, которая всегда откатывается, а `session.commit()` внутри становится
освобождением savepoint — то есть коммиты в тестах проверяются по-настоящему,
но за пределы теста не попадают.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from seshat.db.base import Base, make_engine
from seshat.db.models import AiCall, AuditLog, User

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(scope="session")
def schema() -> None:
    """Один раз пересоздаёт схему в отдельном цикле событий.

    Асинхронная session-scoped фикстура тут не годится: pytest-asyncio отдаёт
    тесту свой event loop, и соединения asyncpg остаются в чужом цикле.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("не задан TEST_DATABASE_URL")

    async def build() -> None:
        engine = make_engine(TEST_DATABASE_URL)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(build())


@pytest_asyncio.fixture
async def engine(schema: None) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(TEST_DATABASE_URL or "")
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Соединение во внешней транзакции, которая всегда откатывается."""
    conn = await engine.connect()
    transaction = await conn.begin()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


@pytest.fixture
def session_factory(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Фабрика, привязанная к транзакции теста.

    Ту же фабрику получает приложение в тестах API. Без этого `commit()` внутри
    зависимости FastAPI пишет в базу по-настоящему, данные протекают между
    тестами, и падают уже совсем другие проверки.
    """
    return async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def committed_user_ids(engine: AsyncEngine) -> AsyncIterator[list[int]]:
    """Удаляет пользователей, которых тест намеренно коммитит вне savepoint."""
    user_ids: list[int] = []
    try:
        yield user_ids
    finally:
        if user_ids:
            async with engine.begin() as conn:
                await conn.execute(
                    AuditLog.__table__.delete().where(AuditLog.user_id.in_(user_ids))
                )
                await conn.execute(AiCall.__table__.delete().where(AiCall.user_id.in_(user_ids)))
                await conn.execute(User.__table__.delete().where(User.id.in_(user_ids)))
