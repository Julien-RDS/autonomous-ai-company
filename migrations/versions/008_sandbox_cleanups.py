"""Audit trail of sandbox cleanups (additive)."""
from alembic import op
import sqlalchemy as sa

revision = '008_sandbox_cleanups'
down_revision = '007_work_reviews'


def upgrade():
    op.create_table(
        'sandbox_cleanups',
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('run_id', sa.String(40), nullable=True),
        sa.Column('container_name', sa.String(80), nullable=False),
        sa.Column('actor', sa.String(100), nullable=False),
        sa.Column('reason', sa.String(500), nullable=False),
        sa.Column('previous_state', sa.String(20), nullable=True),
        sa.Column('outcome', sa.String(40), nullable=False),
        sa.Column('container_removed', sa.Boolean(), nullable=False),
        sa.Column('workspace_removed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['sandbox_runs.id'], name='fk_cleanup_run'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sandbox_cleanups_run_id', 'sandbox_cleanups', ['run_id'])


def downgrade():
    raise RuntimeError('Cleanup evidence must be retained; restore a backup')
