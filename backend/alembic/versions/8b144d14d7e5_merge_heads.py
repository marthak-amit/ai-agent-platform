"""merge heads

Revision ID: 8b144d14d7e5
Revises: 0035, 7e4ea1ed451e
Create Date: 2026-06-13 16:55:45.351893

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8b144d14d7e5'
down_revision: Union[str, None] = ('0035', '7e4ea1ed451e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
