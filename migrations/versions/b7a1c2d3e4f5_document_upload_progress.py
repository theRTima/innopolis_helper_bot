"""Track uploaded file checksums and processing progress."""

import sqlalchemy as sa
from alembic import op

revision = "b7a1c2d3e4f5"
down_revision = "7a6c1d4e2b90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "document_import_jobs", sa.Column("checksum_sha256", sa.String(64))
    )
    op.add_column(
        "document_import_jobs",
        sa.Column(
            "progress_percent",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "document_import_jobs",
        sa.Column(
            "progress_current",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "document_import_jobs",
        sa.Column(
            "progress_total",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "document_import_jobs", sa.Column("progress_stage", sa.String(32))
    )
    op.create_index(
        "ix_document_import_jobs_checksum_sha256",
        "document_import_jobs",
        ["checksum_sha256"],
    )
    op.execute(
        "UPDATE document_import_jobs "
        "SET progress_percent = 100, progress_stage = 'completed' "
        "WHERE status = 'completed'"
    )
    op.execute(
        "UPDATE document_import_jobs "
        "SET progress_stage = 'failed' WHERE status = 'failed'"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_import_jobs_checksum_sha256",
        table_name="document_import_jobs",
    )
    op.drop_column("document_import_jobs", "progress_stage")
    op.drop_column("document_import_jobs", "progress_total")
    op.drop_column("document_import_jobs", "progress_current")
    op.drop_column("document_import_jobs", "progress_percent")
    op.drop_column("document_import_jobs", "checksum_sha256")
