from dataclasses import replace
from datetime import timedelta
import json
import multiprocessing
import os
from pathlib import Path
import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from company.config import Settings
from company.database import Task, WorkItem, Message, utcnow
from company.budget import Budget
from company.workflow import Board
from company.approvals import approve_task
from company.queue import Queue
from company.worker import work_once
from company import prototyping
from test_migrations import migrated_engine, migrate
from test_crash import pg_url


def prepare(engine, tmp_path, monkeypatch):
    migrate(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    settings=replace(Settings.load(),mock=True)
    Board(sessions,settings,Budget(sessions,settings)).run_once()
    with sessions() as db:
        task_id=db.scalar(select(Task.id).where(Task.category=='prototype'))
    monkeypatch.setattr(prototyping,'ROOT',tmp_path)
    return sessions,settings,task_id


def test_approval_queue_and_idempotent_files(migrated_engine,tmp_path,monkeypatch):
    sessions,settings,task_id=prepare(migrated_engine,tmp_path,monkeypatch)
    q=Queue(sessions)
    with pytest.raises(ValueError):
        q.submit_prototype(task_id)
    approve_task(sessions,task_id)
    key=q.submit_prototype(task_id)
    assert q.submit_prototype(task_id)==key
    assert work_once(sessions,settings,'prototype-worker')
    provider,paths=prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    before=[p.stat().st_mtime_ns for p in paths]
    prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    assert before==[p.stat().st_mtime_ns for p in paths]
    with sessions() as db:
        assert db.get(WorkItem,key).state=='SUCCEEDED'
        assert db.get(Task,task_id).status=='awaiting_evaluation'
        assert db.scalar(select(func.count()).select_from(Message).where(Message.stage=='prototype'))==1
    paths[0].write_text('human edit')
    with pytest.raises(ValueError,match='aucune réécriture'):
        prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    assert paths[0].read_text()=='human edit'


def test_shared_lock_and_publication_failure(migrated_engine,tmp_path,monkeypatch):
    sessions,settings,task_id=prepare(migrated_engine,tmp_path,monkeypatch)
    approve_task(sessions,task_id)
    with prototyping.prototype_lock(sessions,task_id):
        with pytest.raises(RuntimeError,match='autre processus'):
            prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    original=prototyping.os.rename
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(prototyping.os,'rename',fail)
    with pytest.raises(OSError):
        prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    assert not (tmp_path/'artifacts'/'prototypes'/task_id).exists()
    monkeypatch.setattr(prototyping.os,'rename',original)
    _,paths=prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    assert len(paths)==2


def publish_then_crash(url,root):
    prototyping.ROOT=Path(root)
    sessions=sessionmaker(create_engine(url,pool_pre_ping=True),expire_on_commit=False)
    settings=replace(Settings.load(),mock=True,db_url=url)
    rename=prototyping.os.rename
    def crash(source,target):
        rename(source,target)
        os._exit(17)
    prototyping.os.rename=crash
    work_once(sessions,settings,'crashing-prototype')


def test_process_crash_after_artifact_publication(migrated_engine,tmp_path,monkeypatch):
    url=pg_url(migrated_engine)
    sessions,settings,task_id=prepare(migrated_engine,tmp_path,monkeypatch)
    approve_task(sessions,task_id)
    key=Queue(sessions).submit_prototype(task_id)
    child=multiprocessing.get_context('spawn').Process(target=publish_then_crash,args=(url,str(tmp_path)))
    child.start(); child.join(30)
    if child.is_alive():
        child.kill(); child.join(); pytest.fail('child timed out')
    assert child.exitcode==17
    with sessions.begin() as db:
        assert db.get(Task,task_id).status=='approved'
        assert db.get(Task,task_id).artifact_manifest is not None
        db.get(WorkItem,key).lease_until=utcnow()-timedelta(seconds=1)
    assert work_once(sessions,settings,'replacement')
    with sessions() as db:
        assert db.get(WorkItem,key).state=='SUCCEEDED'
        assert db.get(Task,task_id).status=='awaiting_evaluation'
        assert db.scalar(select(func.count()).select_from(Message).where(Message.stage=='prototype'))==1


def test_status_alone_does_not_replace_human_approval(migrated_engine,tmp_path,monkeypatch):
    sessions,settings,task_id=prepare(migrated_engine,tmp_path,monkeypatch)
    with sessions.begin() as db:
        db.get(Task,task_id).status='approved'
    with pytest.raises(ValueError,match='humaine enregistrée'):
        prototyping.generate_prototype(sessions,settings,Budget(sessions,settings),task_id)
    with pytest.raises(ValueError,match='humaine enregistrée'):
        Queue(sessions).submit_prototype(task_id)
    approve_task(sessions,task_id)
    assert Queue(sessions).submit_prototype(task_id)
