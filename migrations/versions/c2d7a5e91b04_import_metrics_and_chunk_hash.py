"""Store chunk fingerprints and basic import timings.

Revision ID: c2d7a5e91b04
Revises: 5e42b8c17a90
"""

from alembic import op
import sqlalchemy as sa

revision = "c2d7a5e91b04"
down_revision = "5e42b8c17a90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_versions", sa.Column("chunks_sha256", sa.String(64), nullable=True))
    op.create_table(
        "import_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("document_version_id", sa.Integer(), sa.ForeignKey("document_versions.id"), nullable=False),
        sa.Column("parser", sa.String(32), nullable=False),
        sa.Column("imported", sa.Boolean(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_hits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parse_ms", sa.Integer(), nullable=False),
        sa.Column("text_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("vision_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
    )
    op.create_index("ix_import_runs_document_id", "import_runs", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_import_runs_document_id", table_name="import_runs")
    op.drop_table("import_runs")
    op.drop_column("document_versions", "chunks_sha256")
