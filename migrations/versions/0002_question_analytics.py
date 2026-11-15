"""Store tags, recurring issues and escalation state for support questions."""

import sqlalchemy as sa
from alembic import op

revision = "0002_question_analytics"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "question_analytics",
        sa.Column(
            "question_id",
            sa.Integer(),
            sa.ForeignKey("questions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("area", sa.String(32)),
        sa.Column("kind", sa.String(32)),
        sa.Column("issue", sa.String(96)),
        sa.Column("classified_at", sa.DateTime(timezone=True)),
        sa.Column("escalation_status", sa.String(16)),
    )
    op.create_index("ix_questions_created_at", "questions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_questions_created_at", table_name="questions")
    op.drop_table("question_analytics")
