"""ws_blips: /ws disconnect (blip) instrumentation

Revision ID: 0002_ws_blips
Revises: 0001_initial
Create Date: 2026-07-26

One row per /ws disconnect (handover §4-7). Inserted when the hub loses the
client, updated in place on reconnect. ``reconnected_at IS NULL`` means the
session never came back (user decision 2 — no sweeper stamps it later).

Column notes:
- ``close_code``/``close_reason`` — from the client's close frame
  (1001 = clean close e.g. device sleep/app background, 1006 = abnormal
  drop e.g. network). NULL when the server detected the loss itself.
- ``detected_by`` — 'client_disconnect' (WebSocketDisconnect on /ws) or
  'keepalive_timeout' (server-side pong timeout).
- ``duration_ms``/``flushed_count``/``lost_count`` — stamped on reconnect.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_ws_blips"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "ws_blips",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column(
            "disconnected_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("reconnected_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("flushed_count", sa.Integer(), nullable=True),
        sa.Column("lost_count", sa.Integer(), nullable=True),
        sa.Column("close_code", sa.Integer(), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("detected_by", sa.Text(), nullable=False),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_ws_blips_session_id",
        "ws_blips",
        ["session_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ws_blips_disconnected_at",
        "ws_blips",
        ["disconnected_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Downgrade schema. The app schema itself is owned by 0001."""
    op.drop_index(
        "ix_ws_blips_disconnected_at", table_name="ws_blips", schema=SCHEMA
    )
    op.drop_index("ix_ws_blips_session_id", table_name="ws_blips", schema=SCHEMA)
    op.drop_table("ws_blips", schema=SCHEMA)
