import os
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, inspect, text
from alembic.config import Config
from alembic import command

@pytest.fixture(params=['sqlite', 'postgresql'])
def migrated_engine(request, tmp_path):
    if request.param == 'postgresql':
        url = os.getenv('TEST_DATABASE_URL')
        if not url:
            pytest.skip('TEST_DATABASE_URL required for real PostgreSQL')
        admin = create_engine(url, pool_pre_ping=True)
        schema = 'test_' + uuid4().hex
        with admin.begin() as c:
            c.execute(text(f'CREATE SCHEMA {schema}'))
        engine = create_engine(url, pool_pre_ping=True, connect_args={'options': f'-csearch_path={schema}'})
    else:
        engine = create_engine(f'sqlite:///{tmp_path}/test.db')
    yield engine
    engine.dispose()
    if request.param == 'postgresql':
        with admin.begin() as c:
            c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def migrate(engine, revision='head'):
    config = Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    with engine.begin() as c:
        config.attributes['connection'] = c
        command.upgrade(config, revision)


def test_fresh_and_repeated_migration(migrated_engine):
    migrate(migrated_engine)
    migrate(migrated_engine)
    assert {'runs','tasks','messages','ledger'} <= set(inspect(migrated_engine).get_table_names())


def test_adopt_historical_database_preserves_rows(migrated_engine):
    migrate(migrated_engine, 'acfdbd6cc55e')
    with migrated_engine.begin() as c:
        c.execute(text("INSERT INTO runs(id, started_at, status, mode, mission) VALUES ('historic', CURRENT_TIMESTAMP, 'completed', 'mock', 'legacy')"))
        c.execute(text("INSERT INTO ledger(id,run_id,created_at,provider,amount_eur,state,details) VALUES ('old','historic',CURRENT_TIMESTAMP,'openai',0.123456789,'settled','legacy')"))
        c.execute(text('DROP TABLE alembic_version'))
    migrate(migrated_engine)
    with migrated_engine.connect() as c:
        assert c.scalar(text("SELECT mission FROM runs WHERE id='historic'")) == 'legacy'
        from company.database import Ledger
        from sqlalchemy import select
        from decimal import Decimal
        assert c.scalar(select(Ledger.amount_eur).where(Ledger.id=='old')) == Decimal('0.123456789')
