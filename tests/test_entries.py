"""Подтверждённое создание Entry и его идемпотентность на PostgreSQL."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest
from sqlalchemy import func, select

from seshat.db.base import make_session_factory, session_scope
from seshat.db.enums import AuditAction, EntryKind
from seshat.db.models import AuditLog, Entry
from seshat.domain.entries import ConfirmationConflictError, create_entry
from seshat.domain.parsing import NormalizedEntry
from seshat.domain.users import get_or_create_user

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="не задан TEST_DATABASE_URL — интеграционные тесты пропущены",
)


def _draft(*, title: str = "Собеседование") -> NormalizedEntry:
    return NormalizedEntry(
        kind=EntryKind.EVENT,
        title=title,
        start_at_utc=dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.UTC),
        tz="Europe/Moscow",
        local_time=dt.time(15, 0),
        reminders_min_before=[60],
    )


async def test_create_entry_writes_entry_and_audit_in_one_transaction(session) -> None:
    user = await get_or_create_user(session, 123456789)
    confirmation_id = uuid.uuid4()

    result = await create_entry(session, user.id, _draft(), confirmation_id=confirmation_id)

    assert result.created is True
    assert result.entry.id is not None
    audit = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.entity == "entry", AuditLog.entity_id == result.entry.id
            )
        )
    ).scalar_one()
    assert audit.action is AuditAction.CREATE
    assert audit.payload["confirmation_id"] == str(confirmation_id)
    assert audit.payload["draft"]["title"] == "Собеседование"


async def test_duplicate_confirmation_returns_same_entry_without_duplicates(session) -> None:
    user = await get_or_create_user(session, 123456789)
    confirmation_id = uuid.uuid4()
    draft = _draft()

    first = await create_entry(session, user.id, draft, confirmation_id=confirmation_id)
    second = await create_entry(session, user.id, draft, confirmation_id=confirmation_id)

    assert first.created is True
    assert second.created is False
    assert second.entry.id == first.entry.id
    entry_count = await session.scalar(select(func.count()).select_from(Entry))
    audit_count = await session.scalar(select(func.count()).select_from(AuditLog))
    assert entry_count == 1
    assert audit_count == 1


async def test_confirmation_cannot_be_reused_for_different_draft(session) -> None:
    user = await get_or_create_user(session, 123456789)
    confirmation_id = uuid.uuid4()
    await create_entry(session, user.id, _draft(), confirmation_id=confirmation_id)

    with pytest.raises(ConfirmationConflictError, match="другой карточки"):
        await create_entry(
            session,
            user.id,
            _draft(title="Другая запись"),
            confirmation_id=confirmation_id,
        )


async def test_concurrent_confirmations_from_two_connections_create_once(
    engine, committed_user_ids
) -> None:
    """Advisory lock защищает также два вызова на разных соединениях."""
    factory = make_session_factory(engine)
    telegram_id = uuid.uuid4().int % 9_000_000_000 + 1_000_000_000
    async with session_scope(factory) as setup_session:
        user = await get_or_create_user(setup_session, telegram_id)
        user_id = user.id
        committed_user_ids.append(user_id)

    confirmation_id = uuid.uuid4()
    draft = _draft(title=f"Конкурентное подтверждение {confirmation_id}")

    async def confirm() -> tuple[int, bool]:
        async with session_scope(factory) as worker_session:
            result = await create_entry(
                worker_session,
                user_id,
                draft,
                confirmation_id=confirmation_id,
            )
            return result.entry.id, result.created

    first, second = await asyncio.gather(confirm(), confirm())

    assert first[0] == second[0]
    assert sorted((first[1], second[1])) == [False, True]
    async with factory() as check_session:
        entry_count = await check_session.scalar(
            select(func.count()).select_from(Entry).where(Entry.title == draft.title)
        )
        audit_count = await check_session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.payload["confirmation_id"].as_string() == str(confirmation_id))
        )
    assert entry_count == 1
    assert audit_count == 1
