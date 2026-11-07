"""Exact monetary storage; retain historical amounts, with explicit rounding."""
from alembic import op
import sqlalchemy as sa
from decimal import Decimal, ROUND_HALF_UP
revision = '002_money'
down_revision = 'acfdbd6cc55e'

def upgrade():
    sqlite = op.get_bind().dialect.name == 'sqlite'
    for table, column in [('ledger','amount_eur'), ('tasks','value_eur')]:
        converted = []
        if sqlite:
            rows = op.get_bind().execute(sa.text(f'SELECT id, {column} FROM {table}')).all()
            for key, value in rows:
                amount = Decimal(str(value)).quantize(Decimal('0.000000001'), rounding=ROUND_HALF_UP)
                if not amount.is_finite() or amount < 0:
                    raise RuntimeError('Montant historique invalide; correction manuelle requise')
                converted.append({'value': int(amount * 1_000_000_000), 'id': key})
        with op.batch_alter_table(table) as b:
            b.alter_column(column, existing_type=sa.Float(), type_=sa.BigInteger() if sqlite else sa.Numeric(18,9),
                           existing_nullable=False)
        if converted:
            op.get_bind().execute(sa.text(f'UPDATE {table} SET {column}=:value WHERE id=:id'), converted)
    with op.batch_alter_table('ledger') as b:
        kind = sa.BigInteger() if sqlite else sa.Numeric(18,9)
        b.add_column(sa.Column('estimated_eur', kind, nullable=True))
        b.add_column(sa.Column('actual_eur', kind, nullable=True))
        b.add_column(sa.Column('currency', sa.String(3), nullable=False, server_default='EUR'))
        b.add_column(sa.Column('operation_key', sa.String(200), nullable=True))
        b.add_column(sa.Column('tariff_version', sa.Text(), nullable=False, server_default='legacy'))
        b.create_unique_constraint('uq_ledger_operation_key', ['operation_key'])
        b.create_check_constraint('ck_ledger_amount', 'amount_eur >= 0')
        b.create_check_constraint('ck_ledger_currency', "currency = 'EUR'")
    # Historical settled values were estimates from token usage, not operator invoices.
    op.execute("UPDATE ledger SET estimated_eur=amount_eur")

def downgrade():
    raise RuntimeError('Restore a backup; monetary downgrade is intentionally disabled')
