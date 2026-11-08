"""Queue approved prototypes without losing historical workflow jobs."""
from alembic import op
import sqlalchemy as sa
revision='004_prototypes'
down_revision='c7f86b8b3fbe'

def upgrade():
    sqlite=op.get_bind().dialect.name=='sqlite'
    with op.batch_alter_table('work_items', naming_convention={'uq':'uq_%(table_name)s_%(column_0_name)s'}) as b:
        b.drop_constraint('uq_work_items_run_id' if sqlite else 'work_items_run_id_key',type_='unique')
        b.add_column(sa.Column('kind',sa.String(20),nullable=False,server_default='workflow'))
        b.add_column(sa.Column('task_id',sa.String(40),nullable=True))
        b.create_foreign_key('fk_work_task','tasks',['task_id'],['id'])
        b.create_unique_constraint('uq_work_task',['task_id'])
        b.create_check_constraint('ck_work_kind',"(kind='workflow' AND task_id IS NULL) OR (kind='prototype' AND task_id IS NOT NULL)")
    with op.batch_alter_table('tasks') as b:
        b.add_column(sa.Column('artifact_manifest',sa.Text(),nullable=True))

def downgrade():
    raise RuntimeError('Restore a backup instead of discarding durable prototype jobs')
