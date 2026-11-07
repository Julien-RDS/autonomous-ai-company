"""Human cost reconciliation evidence."""
from alembic import op
import sqlalchemy as sa
revision='005_cost_review'
down_revision='004_prototypes'

def upgrade():
    with op.batch_alter_table('ledger') as b:
        b.add_column(sa.Column('reconciliation_reference',sa.Text(),nullable=True))

def downgrade():
    raise RuntimeError('Audit evidence must not be silently discarded')
