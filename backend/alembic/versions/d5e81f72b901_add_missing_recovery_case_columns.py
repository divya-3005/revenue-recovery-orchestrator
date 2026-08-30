"""Add missing recovery_case columns

Revision ID: d5e81f72b901
Revises: a1c5befc3a02
Create Date: 2026-08-30 19:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e81f72b901'
down_revision: Union[str, Sequence[str], None] = 'a1c5befc3a02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('recovery_cases')]

    if 'customer_email' not in columns:
        op.add_column('recovery_cases', sa.Column('customer_email', sa.String(length=255), nullable=True))
    if 'customer_phone' not in columns:
        op.add_column('recovery_cases', sa.Column('customer_phone', sa.String(length=50), nullable=True))
    if 'latest_diagnosis_confidence' not in columns:
        op.add_column('recovery_cases', sa.Column('latest_diagnosis_confidence', sa.Float(), nullable=True))
    if 'latest_channel' not in columns:
        op.add_column('recovery_cases', sa.Column('latest_channel', sa.String(length=50), nullable=True))
    if 'last_notified_at' not in columns:
        op.add_column('recovery_cases', sa.Column('last_notified_at', sa.DateTime(timezone=True), nullable=True))
    if 'pending_decision_id' not in columns:
        op.add_column('recovery_cases', sa.Column('pending_decision_id', sa.String(length=255), nullable=True))
    if 'pending_decision_hash' not in columns:
        op.add_column('recovery_cases', sa.Column('pending_decision_hash', sa.String(length=255), nullable=True))
    if 'opted_out' not in columns:
        op.add_column('recovery_cases', sa.Column('opted_out', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('recovery_cases', 'opted_out')
    op.drop_column('recovery_cases', 'pending_decision_hash')
    op.drop_column('recovery_cases', 'pending_decision_id')
    op.drop_column('recovery_cases', 'last_notified_at')
    op.drop_column('recovery_cases', 'latest_channel')
    op.drop_column('recovery_cases', 'latest_diagnosis_confidence')
    op.drop_column('recovery_cases', 'customer_phone')
    op.drop_column('recovery_cases', 'customer_email')
