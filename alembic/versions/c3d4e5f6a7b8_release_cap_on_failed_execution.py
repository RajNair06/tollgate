"""release cap on definitely-failed executions

Adds actions.execution_failed_at: set when an allowed action's PayPal
execution fails DEFINITELY (4xx business rejection — provably not executed).
The rolling-cap sum excludes such rows, releasing the reservation.
Ambiguous failures (timeout, 5xx) leave it NULL and keep the reservation.

Revision ID: c3d4e5f6a7b8
Revises: 7f9c7b6f0fe1
Create Date: 2026-11-07
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = '7f9c7b6f0fe1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "actions",
        sa.Column("execution_failed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("actions", "execution_failed_at")
