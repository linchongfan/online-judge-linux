"""submission status index + judge task nonce

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-11-17

"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Global queue-capacity checks filter submissions by status alone.
    op.create_index('ix_submission_status', 'submission', ['status'])
    # Persist the per-dispatch nonce so result consumers can verify it
    # instead of treating it as an unverified echo field.
    op.add_column('judge_task', sa.Column('nonce', sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column('judge_task', 'nonce')
    op.drop_index('ix_submission_status', table_name='submission')