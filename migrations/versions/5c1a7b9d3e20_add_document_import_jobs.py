"""Add the admin document upload queue.

Revision ID: 5c1a7b9d3e20
Revises: a8d6c21f0b43
"""

from alembic import op
import sqlalchemy as sa


revision = "5c1a7b9d3e20"
down_revision = "a8d6c21f0b43"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_import_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("original_name", sa.String(length=500), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'failed')",
            name="ck_document_import_jobs_status",
        ),
    )
    op.create_index("ix_document_import_jobs_document_id", "document_import_jobs", ["document_id"])
    op.create_index("ix_document_import_jobs_status", "document_import_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_document_import_jobs_status", table_name="document_import_jobs")
    op.drop_index("ix_document_import_jobs_document_id", table_name="document_import_jobs")
    op.drop_table("document_import_jobs")
