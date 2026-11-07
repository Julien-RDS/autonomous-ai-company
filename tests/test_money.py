from datetime import timedelta
from decimal import Decimal
from dataclasses import replace
import pytest
from sqlalchemy.orm import sessionmaker
from company.database import Run, Ledger, utcnow
from company.config import Settings
from company.budget import Budget, BudgetExceeded
from company.money import money
from test_migrations import migrated_engine, migrate


def test_rounding_and_currency():
    assert money('0.0000000005') == Decimal('0.000000001')
    assert money('0.1') + money('0.2') == money('0.3')
    with pytest.raises(ValueError):
        money('10', 'USD')


def test_exact_reservation_and_crash_reconciliation(migrated_engine, monkeypatch):
    monkeypatch.setenv('MOCK_MODE','true')
    migrate(migrated_engine)
    sessions = sessionmaker(migrated_engine, expire_on_commit=False)
    settings = replace(Settings.load(), monthly_limit=Decimal('0.3'), run_limit=Decimal('0.3'),
                       prices={'openai':(Decimal('1'), Decimal('2'))})
    budget = Budget(sessions, settings)
    with sessions.begin() as db:
        run = Run(mode='mock',mission='money'); db.add(run); db.flush(); run_id=run.id
    a = budget.reserve(run_id, 'openai', Decimal('0.1'), operation_key='a')
    budget.reserve(run_id, 'openai', Decimal('0.2'), operation_key='b')
    assert budget.totals() == Decimal('0.3')
    with pytest.raises(BudgetExceeded):
        budget.reserve(run_id, 'openai', Decimal('0.000000001'))
    with pytest.raises(RuntimeError):
        budget.reserve(run_id, 'openai', Decimal('0.1'), operation_key='a')
    assert budget.reconcile_abandoned(utcnow()+timedelta(seconds=1)) == 2
    assert budget.reconcile_abandoned(utcnow()+timedelta(seconds=1)) == 0
    assert budget.totals() == Decimal('0.3')
    with sessions() as db:
        assert db.get(Ledger,a).actual_eur is None
    assert budget.actual_cost('openai', 100000, 100000) == Decimal('0.3')


def test_concurrent_budget_reservations(migrated_engine, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    monkeypatch.setenv('MOCK_MODE','true')
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    settings=replace(Settings.load(),monthly_limit=Decimal('0.1'))
    budget=Budget(sessions,settings)
    with sessions.begin() as db:
        run=Run(mode='mock',mission='concurrency'); db.add(run); db.flush(); run_id=run.id
    barrier=Barrier(2)
    def reserve(_):
        barrier.wait()
        try:
            budget.reserve(run_id,'openai',Decimal('0.08'))
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reserve,range(2)))==[False,True]
    assert budget.totals()==Decimal('0.08')
