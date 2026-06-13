"""merge heads

Revision ID: 7e4ea1ed451e
Revises: 0034, 7ad7a16a821a
Create Date: 2026-06-13 04:31:15.330366

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7e4ea1ed451e'
down_revision: Union[str, None] = ('0034', '7ad7a16a821a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
