from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from company.database import WorkItem, Attempt, utcnow
from company.queue import Queue, LostLease
from test_migrations import migrated_engine, migrate


def test_queue_concurrency_idempotence_and_fencing(migrated_engine):
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions)
    key=q.submit('one','mission')
    assert q.submit('one','mission')==key
    with ThreadPoolExecutor(2) as pool:
        claims=list(pool.map(q.claim,['a','b']))
    assert sum(c is not None for c in claims)==1
    old=next(c for c in claims if c)
    with sessions.begin() as db:
        db.get(WorkItem,key).lease_until=utcnow()-timedelta(seconds=1)
    new=q.claim('replacement')
    assert new.id==key and new.token!=old.token
    with pytest.raises(LostLease):
        q.finish(key,old.token)
    q.heartbeat(key,new.token)
    q.finish(key,new.token)
    assert q.claim('another') is None
    with sessions() as db:
        assert db.get(WorkItem,key).state=='SUCCEEDED'
        assert len(db.scalars(select(Attempt)).all())==2


def test_exhaustion_and_ambiguous_external_effect(migrated_engine):
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions)
    key=q.submit('dead','mission',max_attempts=1)
    item=q.claim('a'); q.finish(item.id,item.token,success=False)
    with sessions() as db:
        assert db.get(WorkItem,key).state=='FAILED'
    key=q.submit('external','mission',external=True)
    item=q.claim('b')
    # External effect could have succeeded before the process disappeared.
    with sessions.begin() as db:
        db.get(WorkItem,key).lease_until=utcnow()-timedelta(seconds=1)
    assert q.claim('c') is None
    with sessions() as db:
        assert db.get(WorkItem,key).state=='WAITING_APPROVAL'
    q.cancel(key)
    with pytest.raises(ValueError):
        q.cancel(key)


def test_retry_backoff_and_history(migrated_engine):
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions); key=q.submit('retry','mission',max_attempts=2)
    item=q.claim('a'); q.finish(key,item.token,success=False)
    assert q.claim('too-soon') is None
    with sessions.begin() as db:
        item=db.get(WorkItem,key)
        assert item.state=='RETRY_SCHEDULED'
        item.scheduled_at=utcnow()-timedelta(seconds=1)
    item=q.claim('b'); q.finish(key,item.token,success=False)
    assert q.claim('c') is None
    with sessions() as db:
        assert db.get(WorkItem,key).state=='FAILED'
        assert len(db.scalars(select(Attempt)).all())==2


def test_database_constraints_and_rollback(migrated_engine):
    from sqlalchemy.exc import IntegrityError
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions); key=q.submit('unique','mission')
    with sessions() as db:
        run_id=db.get(WorkItem,key).run_id
    with pytest.raises(IntegrityError):
        with sessions.begin() as db:
            db.add(WorkItem(run_id=run_id,idempotency_key='unique'))
    if migrated_engine.dialect.name=='postgresql':
        with pytest.raises(IntegrityError):
            with sessions.begin() as db:
                db.add(WorkItem(run_id='nonexistent',idempotency_key='orphan'))
    with pytest.raises(IntegrityError):
        with sessions.begin() as db:
            db.get(WorkItem,key).state='INVALID'
    with sessions() as db:
        assert db.get(WorkItem,key).state=='PENDING'
