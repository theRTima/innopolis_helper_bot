"""add separate parser content to chunks

Revision ID: 5e42b8c17a90
Revises: 9903fef3f798
Create Date: 2026-11-14

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5e42b8c17a90"
down_revision: Union[str, Sequence[str], None] = "9903fef3f798"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "content")
