"""Add payment_confirmations and approval fields

Revision ID: a1c5befc3a02
Revises: 6a89ee1c9229
Create Date: 2026-08-30 15:29:19.256360

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c5befc3a02'
down_revision: Union[str, Sequence[str], None] = '6a89ee1c9229'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    # 1. Update Enum casestatus to include PAYMENT_PENDING and PARTIALLY_RECOVERED
    op.execute("ALTER TYPE casestatus ADD VALUE IF NOT EXISTS 'PAYMENT_PENDING'")
    op.execute("ALTER TYPE casestatus ADD VALUE IF NOT EXISTS 'PARTIALLY_RECOVERED'")

    if 'payment_confirmations' not in tables:
        op.create_table('payment_confirmations',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('payment_id', sa.String(), nullable=False),
        sa.Column('case_id', sa.String(), nullable=False),
        sa.Column('amount_paise', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('confirmed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['case_id'], ['recovery_cases.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_payment_confirmations_case_id'), 'payment_confirmations', ['case_id'], unique=False)
        op.create_index(op.f('ix_payment_confirmations_confirmed_at'), 'payment_confirmations', ['confirmed_at'], unique=False)
        op.create_index(op.f('ix_payment_confirmations_payment_id'), 'payment_confirmations', ['payment_id'], unique=True)
        
    columns = [c['name'] for c in inspector.get_columns('recovery_cases')]
    
    if 'approval_status' not in columns:
        approval_status_enum = sa.Enum('NOT_REQUIRED', 'PENDING', 'APPROVED', 'REJECTED', name='approvalstatus')
        approval_status_enum.create(op.get_bind(), checkfirst=True)
        op.add_column('recovery_cases', sa.Column('approval_status', approval_status_enum, nullable=True))
        op.execute("UPDATE recovery_cases SET approval_status = 'NOT_REQUIRED'")
        op.alter_column('recovery_cases', 'approval_status', nullable=False)
        
    if 'approved_decision_id' not in columns:
        op.add_column('recovery_cases', sa.Column('approved_decision_id', sa.String(length=255), nullable=True))
    if 'approved_decision_hash' not in columns:
        op.add_column('recovery_cases', sa.Column('approved_decision_hash', sa.String(length=255), nullable=True))
    if 'approved_at' not in columns:
        op.add_column('recovery_cases', sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True))
    if 'approved_by' not in columns:
        op.add_column('recovery_cases', sa.Column('approved_by', sa.String(length=255), nullable=True))
        
    if 'recovered_amount_paise' not in columns:
        op.add_column('recovery_cases', sa.Column('recovered_amount_paise', sa.Integer(), nullable=True))
        op.execute("UPDATE recovery_cases SET recovered_amount_paise = 0")
        op.alter_column('recovery_cases', 'recovered_amount_paise', nullable=False)
        
    if 'payment_confirmed_at' not in columns:
        op.add_column('recovery_cases', sa.Column('payment_confirmed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('recovery_cases', 'payment_confirmed_at')
    op.drop_column('recovery_cases', 'recovered_amount_paise')
    op.drop_column('recovery_cases', 'approved_by')
    op.drop_column('recovery_cases', 'approved_at')
    op.drop_column('recovery_cases', 'approved_decision_hash')
    op.drop_column('recovery_cases', 'approved_decision_id')
    op.drop_column('recovery_cases', 'approval_status')
    op.drop_index(op.f('ix_payment_confirmations_payment_id'), table_name='payment_confirmations')
    op.drop_index(op.f('ix_payment_confirmations_confirmed_at'), table_name='payment_confirmations')
    op.drop_index(op.f('ix_payment_confirmations_case_id'), table_name='payment_confirmations')
    op.drop_table('payment_confirmations')
