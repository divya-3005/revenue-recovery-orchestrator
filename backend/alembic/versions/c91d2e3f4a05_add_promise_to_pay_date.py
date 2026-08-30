"""Add promise_to_pay_date to recovery_cases

Revision ID: c91d2e3f4a05
Revises: b8ad0914f966
Create Date: 2026-08-30 13:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c91d2e3f4a05'
down_revision: Union[str, Sequence[str], None] = '948e0f799ac8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add promise_to_pay_date column — nullable, stores the date the customer committed to pay by."""
    op.add_column('recovery_cases', sa.Column('promise_to_pay_date', sa.Date(), nullable=True))


def downgrade() -> None:
    """Remove promise_to_pay_date column."""
    op.drop_column('recovery_cases', 'promise_to_pay_date')
