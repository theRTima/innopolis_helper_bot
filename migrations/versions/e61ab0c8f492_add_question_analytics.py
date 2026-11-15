"""Record answer and operator states per question.

Revision ID: e61ab0c8f492
Revises: c2d7a5e91b04
"""

from alembic import op
import sqlalchemy as sa

revision = "e61ab0c8f492"
down_revision = "c2d7a5e91b04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analytics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id"), unique=True, nullable=False),
        sa.Column("answer_status", sa.String(32)),
        sa.Column("operator_status", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("answer_status IN ('with_sources', 'no_evidence', 'unverified', 'error') OR answer_status IS NULL", name="ck_analytics_answer_status"),
        sa.CheckConstraint("operator_status IN ('requested', 'delivered', 'failed') OR operator_status IS NULL", name="ck_analytics_operator_status"),
    )
    op.execute("INSERT INTO analytics (question_id, answer_status) SELECT id, CASE WHEN sources != '[]' THEN 'with_sources' ELSE 'unverified' END FROM questions")


def downgrade() -> None:
    op.drop_table("analytics")
