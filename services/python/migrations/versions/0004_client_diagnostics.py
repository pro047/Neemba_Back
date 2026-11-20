"""client_diagnostics: device-side log lines uploaded by the app

Revision ID: 0004_client_diagnostics
Revises: 0003_ws_blips_client_id
Create Date: 2026-11-18

One row per uploaded log line. ``(install_id, seq)`` is the idempotency key:
the app resends a batch after an ambiguous 503, and the unique constraint plus
``ON CONFLICT DO NOTHING`` keeps that from duplicating rows.

``client_ts`` is the device wall clock parsed from the line head — NOT a sort
key within a device (use ``seq``). It exists for the ±5 min join with
``ws_blips``. Only new objects are created; existing tables are untouched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_client_diagnostics"
down_revision: Union[str, Sequence[str], None] = "0003_ws_blips_client_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "client_diagnostics",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("install_id", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column(
            "line_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("client_ts", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("event", sa.Text(), nullable=True),
        sa.Column("app_version", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "install_id", "seq", name="uq_client_diagnostics_install_seq"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_client_diagnostics_client_ts",
        "client_diagnostics",
        ["client_ts"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_client_diagnostics_received_at",
        "client_diagnostics",
        ["received_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Downgrade schema. The app schema itself is owned by 0001."""
    op.drop_index(
        "ix_client_diagnostics_received_at",
        table_name="client_diagnostics",
        schema=SCHEMA,
    )
    op.drop_index(
        "ix_client_diagnostics_client_ts",
        table_name="client_diagnostics",
        schema=SCHEMA,
    )
    op.drop_table("client_diagnostics", schema=SCHEMA)
