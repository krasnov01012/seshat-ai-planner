"""Track notifications included in a morning digest.

Revision ID: e4f1a8b7c2d0
Revises: c7a9d4e21b63
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4f1a8b7c2d0"
down_revision: str | None = "c7a9d4e21b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("digest_included_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column(
            "digest_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "notifications",
        sa.Column("digest_next_attempt_at_utc", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_notifications_notification_digest_attempt_count_nonnegative",
        "notifications",
        "digest_attempt_count >= 0",
    )
    op.create_index(
        "ix_notifications_digest",
        "notifications",
        [
            "user_id",
            "silent",
            "digest_included_at_utc",
            "digest_next_attempt_at_utc",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_digest", table_name="notifications")
    op.drop_constraint(
        "ck_notifications_notification_digest_attempt_count_nonnegative",
        "notifications",
        type_="check",
    )
    op.drop_column("notifications", "digest_next_attempt_at_utc")
    op.drop_column("notifications", "digest_attempt_count")
    op.drop_column("notifications", "digest_included_at_utc")
