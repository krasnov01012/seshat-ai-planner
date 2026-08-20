"""Transaction-scoped lock order for occurrence actions."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

_OCCURRENCE_LOCK_NAMESPACE = 0x5345000000000000
_USER_CONTEXT_LOCK_NAMESPACE = 0x5346000000000000


async def lock_occurrence_action(session: AsyncSession, occurrence_id: int) -> None:
    """Serializes every path that mutates one occurrence and its notifications."""
    if occurrence_id < 1:
        raise ValueError("occurrence_id must be positive")
    await session.execute(
        select(func.pg_advisory_xact_lock(_OCCURRENCE_LOCK_NAMESPACE + occurrence_id))
    )


async def lock_user_context(session: AsyncSession, user_id: int) -> None:
    """Prevents replacement of the one-row active context during a reaction."""
    if user_id < 1:
        raise ValueError("user_id must be positive")
    await session.execute(
        select(func.pg_advisory_xact_lock(_USER_CONTEXT_LOCK_NAMESPACE + user_id))
    )


__all__ = ["lock_occurrence_action", "lock_user_context"]
