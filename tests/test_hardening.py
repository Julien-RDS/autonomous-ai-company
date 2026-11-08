"""Régressions de la revue Lot 3 (priorité 1)."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from company import prototyping
from company.approvals import approve_task
from company.budget import Budget
from company.config import Settings
from company.database import Attempt, Run, Task, WorkItem, utcnow
from company.queue import MAX_BUSY_DEFERRALS, Busy, Queue
from company.workflow import Board
from company.worker import checkpoints, work_once
from test_crash import pg_url
from test_migrations import migrate, migrated_engine  # noqa: F401


def test_settings_repr_hides_secrets(monkeypatch):
    monkeypatch.setenv('MOCK_MODE', 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-CANARY-KEY')
    monkeypatch.setenv('TAVILY_API_KEY', 'tvly-CANARY')
    monkeypatch.setenv('DATABASE_URL', 'postgresql+psycopg://u:CANARY-PASSWORD@db/x')
    settings = Settings.load()
    text = repr(settings) + str(settings)
    assert 'CANARY' not in text
    assert settings.keys['openai'] == 'sk-CANARY-KEY'  # still usable


def _expire(sessions, key):
    with sessions.begin() as db:
        item = db.get(WorkItem, key)
        item.lease_until = utcnow() - timedelta(seconds=1)
        item.scheduled_at = utcnow() - timedelta(seconds=1)


def _prototype_item(engine, tmp_path, monkeypatch):
    migrate(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = replace(Settings.load(), mock=True)
    Board(sessions, settings, Budget(sessions, settings)).run_once()
    with sessions() as db:
        task_id = db.scalar(select(Task.id).where(Task.category == 'prototype'))
    monkeypatch.setattr(prototyping, 'ROOT', tmp_path)
    approve_task(sessions, task_id, actor='test-human')
    return sessions, settings, task_id, Queue(sessions).submit_prototype(task_id)


def test_live_previous_owner_defers_instead_of_dead_letter(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, task_id, key = _prototype_item(migrated_engine, tmp_path, monkeypatch)
    # A previous owner lost its lease but still holds the prototype lock.
    with prototyping.prototype_lock(sessions, task_id):
        for _ in range(5):
            assert work_once(sessions, settings, 'replacement')
            with sessions() as db:
                item = db.get(WorkItem, key)
                assert item.state == 'RETRY_SCHEDULED' and item.attempts == 0
            _expire(sessions, key)
    assert work_once(sessions, settings, 'after-release')
    with sessions() as db:
        assert db.get(WorkItem, key).state == 'SUCCEEDED'
        outcomes = [a.outcome for a in db.scalars(select(Attempt).where(Attempt.work_id == key))]
    assert outcomes.count('BUSY') == 5 and outcomes.count('SUCCEEDED') == 1


def test_contention_is_bounded_and_handed_to_human(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, task_id, key = _prototype_item(migrated_engine, tmp_path, monkeypatch)
    with prototyping.prototype_lock(sessions, task_id):
        for _ in range(MAX_BUSY_DEFERRALS):
            assert work_once(sessions, settings, 'replacement')
            _expire(sessions, key)
    with sessions() as db:
        item = db.get(WorkItem, key)
        assert item.state == 'WAITING_APPROVAL' and item.attempts == 0
    assert not work_once(sessions, settings, 'never-auto-replayed')


def test_defer_busy_requires_current_lease(migrated_engine):
    from company.queue import LostLease
    migrate(migrated_engine)
    sessions = sessionmaker(migrated_engine, expire_on_commit=False)
    q = Queue(sessions)
    key = q.submit('busy-fence', 'mission')
    old = q.claim('old')
    _expire(sessions, key)
    new = q.claim('new')
    with pytest.raises(LostLease):
        q.defer_busy(key, old.token)
    assert q.defer_busy(key, new.token) == 'RETRY_SCHEDULED'


def test_live_old_workflow_owner_does_not_exhaust_attempts(migrated_engine):
    url = pg_url(migrated_engine)
    migrate(migrated_engine)
    sessions = sessionmaker(migrated_engine, expire_on_commit=False)
    settings = replace(Settings.load(), mock=True, db_url=url)
    q = Queue(sessions)
    key = q.submit('old-owner-alive', 'mission')
    old = q.claim('old')
    with checkpoints(url, old.run_id):
        for _ in range(4):
            _expire(sessions, key)
            assert work_once(sessions, settings, 'replacement')
    with sessions() as db:
        item = db.get(WorkItem, key)
        # Only the old owner's expired attempt counts; contention does not.
        assert item.state == 'RETRY_SCHEDULED' and item.attempts == 1
    _expire(sessions, key)
    assert work_once(sessions, settings, 'replacement')
    with sessions() as db:
        assert db.get(WorkItem, key).state == 'SUCCEEDED'
        assert db.get(Run, old.run_id).status == 'completed'


def test_busy_is_runtime_error_for_cli_reporting():
    assert issubclass(Busy, RuntimeError)


def test_cli_actor_is_recorded_and_validated(migrated_engine):
    from company.__main__ import human_actor
    from company.database import Approval
    assert human_actor('alice') == 'alice'
    assert human_actor(None)
    for bad in ('', ' ', 'x' * 101, 'bad\nactor'):
        with pytest.raises(ValueError):
            human_actor(bad)
    migrate(migrated_engine)
    sessions = sessionmaker(migrated_engine, expire_on_commit=False)
    settings = replace(Settings.load(), mock=True)
    Board(sessions, settings, Budget(sessions, settings)).run_once()
    with sessions() as db:
        task_id = db.scalar(select(Task.id).where(Task.category == 'prototype'))
    approve_task(sessions, task_id, actor=human_actor('alice'))
    with sessions() as db:
        assert db.scalar(select(Approval.actor).where(Approval.task_id == task_id)) == 'alice'
