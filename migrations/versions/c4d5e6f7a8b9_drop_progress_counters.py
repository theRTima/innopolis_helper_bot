"""Drop numeric progress counters; keep percent and stage."""

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b7a1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("document_import_jobs", "progress_total")
    op.drop_column("document_import_jobs", "progress_current")


def downgrade() -> None:
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
