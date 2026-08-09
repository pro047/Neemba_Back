"""ws_blips.client_id: one blip row per socket (multi-listener P1, D5)

Revision ID: 0003_ws_blips_client_id
Revises: 0002_ws_blips
Create Date: 2026-08-08

Until P1 a session held exactly one /ws socket, so ``session_id`` alone
identified a blip's owner. With N listeners per session that stops being
true — several open rows can share a session and nothing tells them apart.

``client_id`` is the hub-issued per-socket id. NULL on every pre-P1 row (and
on any blip recorded without one), which is why the column is nullable: no
backfill, and the rollback is a single ``drop_column``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_ws_blips_client_id"
down_revision: Union[str, Sequence[str], None] = "0002_ws_blips"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "ws_blips",
        sa.Column("client_id", sa.Text(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("ws_blips", "client_id", schema=SCHEMA)
