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


def test_human_reconciliation_and_uncertain_settlement(migrated_engine,monkeypatch):
    from company.database import AuditEvent
    from sqlalchemy import select, func
    monkeypatch.setenv('MOCK_MODE','true')
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    budget=Budget(sessions,Settings.load())
    with sessions.begin() as db:
        run=Run(mode='mock',mission='review'); db.add(run); db.flush(); run_id=run.id
    reservation=budget.reserve(run_id,'openai',Decimal('0.1'),operation_key='uncertain')
    with pytest.raises(ValueError):
        budget.reconcile_unknown(reservation,Decimal('0'),'invoice-1')
    budget.settle(reservation,Decimal('0.01'),error=True)
    assert budget.totals()==Decimal('0.1')
    with pytest.raises(ValueError):
        budget.reconcile_unknown(reservation,Decimal('0'),'')
    budget.reconcile_unknown(reservation,Decimal('0.02'),'invoice-1')
    budget.reconcile_unknown(reservation,Decimal('0.02'),'invoice-1')
    assert budget.totals()==Decimal('0.02')
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.kind=='human_cost_reconciled'))==1
    with pytest.raises(RuntimeError):
        budget.reserve(run_id,'openai',Decimal('0.1'),operation_key='uncertain')
    with pytest.raises(ValueError):
        budget.reconcile_unknown(reservation,Decimal('0.01'),'different-invoice')


def test_rounding_cannot_overflow_numeric_storage():
    with pytest.raises(ValueError,match='arrondi hors domaine'):
        money('999999999.9999999995')


def test_largest_representable_amount_is_preserved():
    assert money('999999999.999999999') == Decimal('999999999.999999999')
    assert money('999999999.9999999994') == Decimal('999999999.999999999')
