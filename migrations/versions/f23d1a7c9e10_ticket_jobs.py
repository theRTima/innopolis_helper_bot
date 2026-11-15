"""Persist ticket requests, retries, provider links and closure times."""

import sqlalchemy as sa
from alembic import op

revision = "f23d1a7c9e10"
down_revision = "9f4c2d7a0b11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column(
            "question_id", sa.Integer(), sa.ForeignKey("questions.id"),
            unique=True,
        ),
        sa.Column("telegram_id", sa.BigInteger()),
        sa.Column("issue_area", sa.String(32)),
        sa.Column("issue_kind", sa.String(32)),
        sa.Column("issue_title", sa.String(96)),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "state", sa.String(16), nullable=False, server_default="queued"
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("external_id", sa.String(255)),
        sa.Column("external_url", sa.Text()),
        sa.Column("external_status", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('support', 'improvement')", name="ck_ticket_jobs_kind"
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'creating', 'created', 'retry', "
            "'uncertain', 'failed', 'closed')",
            name="ck_ticket_jobs_state",
        ),
        sa.UniqueConstraint("kind", "issue_area", "issue_kind", "issue_title",
                            name="uq_ticket_jobs_issue"),
    )
    op.create_index(
        "ix_ticket_jobs_due", "ticket_jobs", ["state", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_ticket_jobs_due", table_name="ticket_jobs")
    op.drop_table("ticket_jobs")
