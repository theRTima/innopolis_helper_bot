"""Track delivery of support ticket creation notices."""

from alembic import op
import sqlalchemy as sa

revision = "7a6c1d4e2b90"
down_revision = "34d5f72abe1a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ticket_jobs", sa.Column("notice_sent_at", sa.DateTime(timezone=True)))
    op.add_column("ticket_jobs", sa.Column("notice_next_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("ticket_jobs", sa.Column("notice_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ticket_jobs", sa.Column("notice_error", sa.String(255)))


def downgrade() -> None:
    op.drop_column("ticket_jobs", "notice_error")
    op.drop_column("ticket_jobs", "notice_attempts")
    op.drop_column("ticket_jobs", "notice_next_attempt_at")
    op.drop_column("ticket_jobs", "notice_sent_at")
