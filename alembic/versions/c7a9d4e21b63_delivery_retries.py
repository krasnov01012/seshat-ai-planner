"""Delivery retry state for restart-safe Telegram notifications.

Revision ID: c7a9d4e21b63
Revises: 9b4d73af0c56
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7a9d4e21b63"
down_revision: str | None = "9b4d73af0c56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # notification_status хранится как VARCHAR без нативного PostgreSQL ENUM:
    # значение `failed` помещается в существующий VARCHAR(9), DDL типа не нужен.
    op.add_column(
        "notifications",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("notifications", sa.Column("next_attempt_at_utc", sa.DateTime(timezone=True)))
    op.add_column("notifications", sa.Column("last_error_code", sa.String(length=64)))
    op.create_check_constraint(
        "ck_notifications_notification_attempt_count_nonnegative",
        "notifications",
        "attempt_count >= 0",
    )


def downgrade() -> None:
    # Старый код не знает значение failed; сохраняем факт недоставки как missed.
    op.execute("UPDATE notifications SET status = 'missed' WHERE status = 'failed'")
    op.drop_constraint(
        "ck_notifications_notification_attempt_count_nonnegative",
        "notifications",
        type_="check",
    )
    op.drop_column("notifications", "last_error_code")
    op.drop_column("notifications", "next_attempt_at_utc")
    op.drop_column("notifications", "attempt_count")
