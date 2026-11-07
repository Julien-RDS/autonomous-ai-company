import multiprocessing
import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import pytest
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import sessionmaker
from company.config import Settings
from company.database import Run, Message, Task, WorkItem, utcnow
from company.budget import Budget
from company.queue import Queue
from company.workflow import Board
from company.worker import checkpoints
from test_migrations import migrated_engine, migrate


def child(url, action, run_id=None):
    engine=create_engine(url,pool_pre_ping=True)
    sessions=sessionmaker(engine,expire_on_commit=False)
    settings=replace(Settings.load(),mock=True,db_url=url)
    if action=='concurrent':
        os._exit(17 if Queue(sessions).claim('racing') else 18)
    if action=='external':
        from pathlib import Path
        assert Queue(sessions).claim('external-worker')
        Path(run_id).write_text('effect-successful')
    elif action=='claim':
        assert Queue(sessions).claim('crashing') is not None
    elif action=='reserve':
        Budget(sessions,settings).reserve(run_id,'openai',Decimal('0.1'),operation_key='crash')
    elif action=='checkpoint':
        with checkpoints(url) as saver:
            board=Board(sessions,settings,Budget(sessions,settings))
            board.graph(saver,interrupt_after=['propose']).invoke(
                {'run_id':run_id,'mission':'crash test','memory':''},
                {'configurable':{'thread_id':run_id}})
    elif action=='resume':
        with checkpoints(url) as saver:
            Board(sessions,settings,Budget(sessions,settings)).run_once(run_id,saver)
        return
    os._exit(17)


def launch(url,action,run_id=None):
    process=multiprocessing.get_context('spawn').Process(target=child,args=(url,action,run_id))
    process.start(); process.join(30)
    if process.is_alive():
        process.kill(); process.join()
        pytest.fail('child exceeded deadline')
    assert process.exitcode==(0 if action=='resume' else 17)


def pg_url(engine):
    if engine.dialect.name!='postgresql':
        pytest.skip('PostgreSQL checkpointer integration')
    with engine.connect() as c:
        schema=c.scalar(text('SELECT current_schema()'))
    return engine.url.update_query_dict({'options':f'-csearch_path={schema}'}).render_as_string(hide_password=False)


def test_process_crash_after_checkpoint_and_resume(migrated_engine):
    url=pg_url(migrated_engine)
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    with sessions.begin() as db:
        run=Run(mode='mock',mission='crash test'); db.add(run); db.flush(); run_id=run.id
    launch(url,'checkpoint',run_id)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Message))==3
    launch(url,'resume',run_id)
    launch(url,'resume',run_id)
    with sessions() as db:
        assert db.get(Run,run_id).status=='completed'
        assert db.scalar(select(func.count()).select_from(Message))==9
        assert db.scalar(select(func.count()).select_from(Task))==2


def test_process_crash_after_claim_and_reservation(migrated_engine):
    url=pg_url(migrated_engine)
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions); key=q.submit('crash','crash test')
    launch(url,'claim')
    with sessions.begin() as db:
        work=db.get(WorkItem,key); run_id=work.run_id
        work.lease_until=utcnow()-timedelta(seconds=1)
    replacement=q.claim('new-process')
    assert replacement.id==key
    launch(url,'reserve',run_id)
    budget=Budget(sessions,Settings.load())
    assert budget.totals()==Decimal('0.1')
    budget.reconcile_abandoned(utcnow()+timedelta(seconds=1))
    with pytest.raises(RuntimeError):
        budget.reserve(run_id,'openai',Decimal('0.1'),operation_key='crash')
    assert budget.totals()==Decimal('0.1')


def test_disconnected_postgres_connection_recovers(migrated_engine):
    url=pg_url(migrated_engine)
    migrate(migrated_engine)
    engine=create_engine(url,pool_pre_ping=True)
    with engine.connect() as c:
        pid=c.scalar(text('SELECT pg_backend_pid()'))
    with migrated_engine.connect() as c:
        assert c.scalar(text('SELECT pg_terminate_backend(:pid)'),{'pid':pid})
    with engine.connect() as c:
        assert c.scalar(text('SELECT 1'))==1
    engine.dispose()


def test_worker_end_to_end(migrated_engine):
    from company.worker import work_once
    url=pg_url(migrated_engine)
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions); key=q.submit('worker','worker integration')
    settings=replace(Settings.load(),mock=True,db_url=url)
    assert work_once(sessions,settings,'worker')
    with sessions() as db:
        item=db.get(WorkItem,key)
        assert item.state=='SUCCEEDED'
        assert db.get(Run,item.run_id).status=='completed'
    assert not work_once(sessions,settings,'worker')


def test_postgres_stop_start_preserves_queue(migrated_engine):
    import subprocess
    import time
    from sqlalchemy.exc import OperationalError
    pg_url(migrated_engine)
    container=os.getenv('TEST_PG_CONTAINER')
    if not container:
        pytest.skip('Disposable TEST_PG_CONTAINER required for server outage test')
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    key=Queue(sessions).submit('outage','recover after server restart')
    try:
        subprocess.run(['docker','stop',container],check=True,capture_output=True,timeout=20)
        with pytest.raises(OperationalError):
            Queue(sessions).claim('offline')
    finally:
        subprocess.run(['docker','start',container],check=True,capture_output=True,timeout=20)
    for _ in range(20):
        try:
            item=Queue(sessions).claim('restarted')
            break
        except OperationalError:
            time.sleep(0.5)
    else:
        pytest.fail('PostgreSQL did not recover')
    assert item.id==key


def test_checkpointed_human_approval_is_idempotent(migrated_engine):
    from company.approvals import approve_task
    from company.database import Approval
    url=pg_url(migrated_engine)
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    settings=replace(Settings.load(),mock=True,db_url=url)
    Board(sessions,settings,Budget(sessions,settings)).run_once()
    with sessions() as db:
        task_id=db.scalar(select(Task.id).where(Task.category=='prototype'))
    with checkpoints(url) as saver:
        assert approve_task(sessions,task_id,checkpointer=saver)['approved']
    with checkpoints(url) as saver:
        assert approve_task(sessions,task_id,checkpointer=saver)['approved']
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Approval))==1
        assert db.get(Task,task_id).status=='approved'


def test_external_effect_crash_is_not_replayed(migrated_engine,tmp_path):
    url=pg_url(migrated_engine); migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions); key=q.submit('external-crash','external simulation',external=True)
    marker=tmp_path/'external-receipt'
    launch(url,'external',str(marker))
    with sessions.begin() as db:
        db.get(WorkItem,key).lease_until=utcnow()-timedelta(seconds=1)
    assert q.claim('replacement') is None
    assert marker.read_text()=='effect-successful'
    with sessions() as db:
        assert db.get(WorkItem,key).state=='WAITING_APPROVAL'


def test_process_concurrency_one_owner(migrated_engine):
    url=pg_url(migrated_engine); migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    Queue(sessions).submit('race','race')
    processes=[multiprocessing.get_context('spawn').Process(target=child,args=(url,'concurrent')) for _ in range(3)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(30)
        if p.is_alive():
            p.kill(); p.join(); pytest.fail('claim timed out')
    assert sorted(p.exitcode for p in processes)==[17,18,18]


def test_live_old_process_cannot_share_workflow_checkpoint(migrated_engine):
    url=pg_url(migrated_engine)
    with checkpoints(url,'same-thread'):
        with pytest.raises(RuntimeError,match='autre processus'):
            with checkpoints(url,'same-thread'):
                pytest.fail('concurrent checkpoint writer')
    with checkpoints(url,'same-thread'):
        pass
