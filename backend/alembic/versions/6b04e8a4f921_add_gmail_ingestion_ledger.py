"""add gmail ingestion ledger

Revision ID: 6b04e8a4f921
Revises: 920b22b1c38c
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6b04e8a4f921"
down_revision: Union[str, None] = "920b22b1c38c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_ingestions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=False),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("thread_id", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("sender", sa.String(length=500), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("classification", sa.String(length=50), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("preview_title", sa.String(length=500), nullable=True),
        sa.Column("body_excerpt", sa.Text(), nullable=True),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_email", "message_id", name="uq_email_ingestion_account_message"),
    )
    op.create_index("ix_email_ingestions_account_email", "email_ingestions", ["account_email"])
    op.create_index("ix_email_ingestions_status", "email_ingestions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_email_ingestions_status", table_name="email_ingestions")
    op.drop_index("ix_email_ingestions_account_email", table_name="email_ingestions")
    op.drop_table("email_ingestions")
