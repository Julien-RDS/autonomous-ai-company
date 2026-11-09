"""Lot 3: single-use execution approvals and bounded sandbox run records."""
from alembic import op
import sqlalchemy as sa

revision = '006_sandbox'
down_revision = '005_cost_review'


def upgrade():
    # Additive only: existing tables and rows are untouched.
    op.create_table(
        'execution_approvals',
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('task_id', sa.String(40), nullable=False),
        sa.Column('manifest_sha256', sa.String(64), nullable=False),
        sa.Column('profile', sa.String(40), nullable=False),
        sa.Column('image_id', sa.String(80), nullable=False),
        sa.Column('actor', sa.String(100), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_exec_approval_task'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_execution_approvals_task_id', 'execution_approvals', ['task_id'])
    op.create_table(
        'sandbox_runs',
        sa.Column('id', sa.String(40), nullable=False),
        sa.Column('task_id', sa.String(40), nullable=False),
        sa.Column('approval_id', sa.String(40), nullable=False),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('profile', sa.String(40), nullable=False),
        sa.Column('image_id', sa.String(80), nullable=False),
        sa.Column('manifest_sha256', sa.String(64), nullable=False),
        sa.Column('container_name', sa.String(80), nullable=False),
        sa.Column('timeout_s', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('exit_code', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('stdout', sa.Text(), nullable=False),
        sa.Column('stderr', sa.Text(), nullable=False),
        sa.Column('output_bytes', sa.Integer(), nullable=False),
        sa.Column('truncated', sa.Boolean(), nullable=False),
        sa.Column('detail', sa.String(250), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], name='fk_sandbox_task'),
        sa.ForeignKeyConstraint(['approval_id'], ['execution_approvals.id'], name='fk_sandbox_approval'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('approval_id', name='uq_sandbox_approval'),
        sa.UniqueConstraint('task_id', 'attempt', name='uq_sandbox_task_attempt'),
        sa.CheckConstraint("state IN ('RUNNING','PASSED','FAILED','TIMEOUT','OUTPUT_LIMIT','OOM','ERROR','INTERRUPTED')",
                           name='ck_sandbox_state'),
        sa.CheckConstraint('attempt > 0 AND timeout_s > 0 AND output_bytes >= 0', name='ck_sandbox_bounds'),
    )
    op.create_index('ix_sandbox_runs_task_id', 'sandbox_runs', ['task_id'])


def downgrade():
    raise RuntimeError('Sandbox evidence must not be silently discarded; restore a backup')
