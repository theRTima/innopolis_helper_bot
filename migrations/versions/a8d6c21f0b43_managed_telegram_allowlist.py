"""Add admin-managed Telegram allowlist.

Revision ID: a8d6c21f0b43
Revises: e61ab0c8f492
"""

from alembic import op
import sqlalchemy as sa


revision = "a8d6c21f0b43"
down_revision = "e61ab0c8f492"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "allowed_telegram_users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=32), nullable=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("username", name="uq_allowed_telegram_username"),
        sa.UniqueConstraint("telegram_id", name="uq_allowed_telegram_id"),
        sa.CheckConstraint(
            "username IS NOT NULL OR telegram_id IS NOT NULL",
            name="ck_allowed_telegram_identity",
        ),
        sa.CheckConstraint(
            "telegram_id IS NULL OR telegram_id > 0",
            name="ck_allowed_telegram_id_positive",
        ),
    )
    op.create_table(
        "access_control_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "access_invites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_by", sa.String(length=100), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), server_default="1", nullable=False),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("max_uses > 0", name="ck_access_invites_max_uses_positive"),
        sa.CheckConstraint("use_count >= 0 AND use_count <= max_uses", name="ck_access_invites_use_count"),
    )
    op.create_table(
        "access_invite_uses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invite_id", sa.Integer(), sa.ForeignKey("access_invites.id"), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("invite_id", "telegram_id", name="uq_access_invite_user"),
    )
    op.create_index("ix_access_invite_uses_invite_id", "access_invite_uses", ["invite_id"])


def downgrade() -> None:
    op.drop_index("ix_access_invite_uses_invite_id", table_name="access_invite_uses")
    op.drop_table("access_invite_uses")
    op.drop_table("access_invites")
    op.drop_table("access_control_state")
    op.drop_table("allowed_telegram_users")
