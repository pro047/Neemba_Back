"""initial monitor schema: app.sessions + app.translations

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-02

Creates the dedicated ``app`` schema and the two tables backing the
translation monitoring page:

- ``app.sessions``    — one row per streaming session (meta + ended_at).
- ``app.translations``— one row per source/translation pair, indexed by
  session_id and created_at for live tailing, ordered history and the
  30-day retention sweep (Phase 7).

Phase 3 ships plain indexed tables. Daily partitioning + the retention
job are deferred to Phase 7 (see docs/monitoring-plan.md §5/§7).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "app"


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("source_lang", sa.Text(), nullable=True),
        sa.Column("target_lang", sa.Text(), nullable=True),
        sa.Column(
            "translation_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "translations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("segment_id", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("source_lang", sa.Text(), nullable=True),
        sa.Column("target_lang", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema=SCHEMA,
    )

    op.create_index(
        "ix_translations_session_id",
        "translations",
        ["session_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_translations_created_at",
        "translations",
        ["created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_translations_created_at", table_name="translations", schema=SCHEMA)
    op.drop_index("ix_translations_session_id", table_name="translations", schema=SCHEMA)
    op.drop_table("translations", schema=SCHEMA)
    op.drop_table("sessions", schema=SCHEMA)
    # Drop the now-empty app schema. The alembic_version table lives in the
    # default (public) schema (see migrations/env.py), so it is untouched by
    # this CASCADE and alembic can record the downgrade afterwards.
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
