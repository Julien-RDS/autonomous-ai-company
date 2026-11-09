"""Versioned reports and immutable outcome evidence; historical rows remain readable."""
from alembic import op
import sqlalchemy as sa
revision='009_result_evidence'
down_revision='008_sandbox_cleanups'

def upgrade():
    for table in ('execution_approvals','sandbox_runs'):
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column('validator_version',sa.String(64),nullable=True))
    with op.batch_alter_table('sandbox_runs') as b:
        b.add_column(sa.Column('summary_json',sa.Text(),nullable=True))
    op.create_table('sandbox_evidence',
        sa.Column('id',sa.String(40),primary_key=True),
        sa.Column('run_id',sa.String(40),sa.ForeignKey('sandbox_runs.id'),nullable=False,unique=True),
        sa.Column('outcome_json',sa.Text(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))

def downgrade():
    raise RuntimeError('Result evidence must be preserved')
