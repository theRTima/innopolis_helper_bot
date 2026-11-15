"""Merge ticket and admin migration heads

Revision ID: 34d5f72abe1a
Revises: 1fd8c20a6b45, f23d1a7c9e10
Create Date: 2026-11-15 20:46:52.843751

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '34d5f72abe1a'
down_revision: Union[str, Sequence[str], None] = ('1fd8c20a6b45', 'f23d1a7c9e10')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
