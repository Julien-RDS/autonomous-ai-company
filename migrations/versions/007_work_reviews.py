"""Human requeue evidence, preserving all attempts."""
from alembic import op
import sqlalchemy as sa
revision='007_work_reviews'
down_revision='006_sandbox'

def upgrade():
    op.create_table('work_reviews',
        sa.Column('id',sa.String(40),primary_key=True),
        sa.Column('work_id',sa.String(40),sa.ForeignKey('work_items.id'),nullable=False),
        sa.Column('actor',sa.String(100),nullable=False),
        sa.Column('reason',sa.String(500),nullable=False),
        sa.Column('previous_state',sa.String(30),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_work_reviews_work_id','work_reviews',['work_id'])

def downgrade():
    raise RuntimeError('Human review history must be retained')
