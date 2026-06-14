"""merge

Revision ID: f58c66651b27
Revises: 0038, 0039_add_variant_material_to_orders
Create Date: 2026-06-14 17:57:57.459588

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f58c66651b27'
down_revision: Union[str, None] = ('0038', '0039_variant_material')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
