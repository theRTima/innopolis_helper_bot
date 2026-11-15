"""Record receipt, delivery and user-confirmed outcome."""
from alembic import op
import sqlalchemy as sa

revision = "4d9a1f2c3b70"
down_revision = "e61ab0c8f492"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("analytics", sa.Column("conversation_id", sa.Integer(), nullable=True))
    op.add_column("analytics", sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("analytics", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("analytics", sa.Column("delivery_status", sa.String(16), nullable=True))
    op.add_column("analytics", sa.Column("resolution", sa.String(16), nullable=True))
    op.create_table("escalation_events",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("question_id", sa.Integer(), sa.ForeignKey("questions.id"), nullable=False),
                    sa.Column("status", sa.String(16), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False))


def downgrade():
    op.drop_table("escalation_events")
    for column in ("resolution", "delivery_status", "sent_at", "received_at", "conversation_id"):
        op.drop_column("analytics", column)
