"""Durable at-least-once workflow delivery with fenced, expiring leases."""
from datetime import timedelta, timezone
import random
from sqlalchemy import select, text
from .database import WorkItem, Run, Attempt, AuditEvent, Task, Approval, utcnow, new_id

class LostLease(RuntimeError):
    pass

class Queue:
    def __init__(self, sessions, lease_seconds=120):
        if lease_seconds < 1:
            raise ValueError('lease_seconds must be positive')
        self.sessions, self.lease_seconds = sessions, lease_seconds

    def _lock(self, db, submit=False):
        if db.get_bind().dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        elif submit:
            db.execute(text('SELECT pg_advisory_xact_lock(7109242)'))

    def submit(self, key, mission, *, external=False, priority=0, scheduled_at=None, max_attempts=3):
        if not key or len(key)>200 or key.startswith('prototype:') or max_attempts<1:
            raise ValueError('Invalid submission')
        with self.sessions.begin() as db:
            self._lock(db, True)
            existing = db.scalar(select(WorkItem).where(WorkItem.idempotency_key == key))
            if existing:
                run = db.get(Run, existing.run_id)
                if existing.kind != 'workflow' or run.mission != mission or existing.external != external:
                    raise ValueError('Idempotency key reused with different payload')
                return existing.id
            run = Run(mission=mission, mode='real' if external else 'mock')
            db.add(run); db.flush()
            item = WorkItem(run_id=run.id,idempotency_key=key,external=external,priority=priority,
                            scheduled_at=scheduled_at or utcnow(),max_attempts=max_attempts)
            db.add(item); db.flush()
            db.add(AuditEvent(entity_id=item.id,kind='submitted'))
            return item.id

    def submit_prototype(self, task_id):
        with self.sessions.begin() as db:
            self._lock(db, True)
            existing = db.scalar(select(WorkItem).where(WorkItem.task_id == task_id))
            if existing:
                return existing.id
            task = db.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or task.category != 'prototype' or task.status != 'approved':
                raise ValueError('Prototype approuvé requis')
            if not db.scalar(select(Approval.id).where(Approval.task_id == task_id)):
                raise ValueError('Validation humaine enregistrée requise')
            run = db.get(Run, task.run_id)
            if run.status != 'completed' or run.mode != 'mock':
                raise ValueError('File de prototypes réservée aux réunions MOCK terminées')
            item = WorkItem(run_id=run.id, task_id=task_id, kind='prototype',
                            idempotency_key=f'prototype:{task_id}')
            db.add(item)
            db.flush()
            db.add(AuditEvent(entity_id=item.id, kind='prototype_submitted'))
            return item.id

    def recover(self):
        with self.sessions.begin() as db:
            self._lock(db)
            now = utcnow()
            items = db.scalars(select(WorkItem).where(WorkItem.state=='RUNNING',
                    WorkItem.lease_until<=now).with_for_update(skip_locked=True)).all()
            for item in items:
                item.state = ('WAITING_APPROVAL' if item.external else
                              'FAILED' if item.attempts>=item.max_attempts else 'RETRY_SCHEDULED')
                item.scheduled_at = now
                attempt = db.get(Attempt,item.token)
                attempt.outcome='LEASE_EXPIRED'; attempt.finished_at=now
                item.token=None; item.owner=None; item.lease_until=None
                db.add(AuditEvent(entity_id=item.id,kind='lease_expired'))
            return len(items)

    def claim(self, owner):
        self.recover()
        with self.sessions.begin() as db:
            self._lock(db)
            item = db.scalar(select(WorkItem).where(
                WorkItem.state.in_(['PENDING','RETRY_SCHEDULED']), WorkItem.scheduled_at<=utcnow(),
                WorkItem.attempts<WorkItem.max_attempts
            ).order_by(WorkItem.priority.desc(),WorkItem.scheduled_at,WorkItem.id)
              .with_for_update(skip_locked=True).limit(1))
            if item is None:
                return None
            item.state='RUNNING'; item.owner=owner; item.token=new_id(); item.attempts+=1
            item.lease_until=utcnow()+timedelta(seconds=self.lease_seconds)
            db.add(Attempt(id=item.token,work_id=item.id,owner=owner))
            db.add(AuditEvent(entity_id=item.id,kind='claimed'))
            db.flush()
            db.expunge(item)
            return item

    def _owned(self, db, work_id, token):
        self._lock(db)
        item=db.scalar(select(WorkItem).where(WorkItem.id==work_id).with_for_update())
        if (item is None or item.state!='RUNNING' or item.token!=token or
            item.lease_until.replace(tzinfo=timezone.utc)<=utcnow()):
            raise LostLease('Lease perdu; résultat refusé')
        return item

    def heartbeat(self, work_id, token):
        with self.sessions.begin() as db:
            item=self._owned(db,work_id,token)
            item.lease_until=utcnow()+timedelta(seconds=self.lease_seconds)

    def finish(self, work_id, token, *, success=True):
        with self.sessions.begin() as db:
            item=self._owned(db,work_id,token)
            item.state=('SUCCEEDED' if success else 'WAITING_APPROVAL' if item.external else
                        'FAILED' if item.attempts>=item.max_attempts else 'RETRY_SCHEDULED')
            if item.state=='RETRY_SCHEDULED':
                item.scheduled_at=utcnow()+timedelta(seconds=min(300,2**min(item.attempts,8))+random.random())
            attempt=db.get(Attempt,token); attempt.finished_at=utcnow(); attempt.outcome=item.state
            item.token=None; item.owner=None; item.lease_until=None
            db.add(AuditEvent(entity_id=item.id,kind=item.state.lower()))

    def cancel(self, work_id):
        with self.sessions.begin() as db:
            self._lock(db)
            item=db.scalar(select(WorkItem).where(WorkItem.id==work_id).with_for_update())
            if item is None or item.state not in ('PENDING','RETRY_SCHEDULED','WAITING_APPROVAL'):
                raise ValueError('Transition interdite')
            item.state='CANCELLED'
            db.add(AuditEvent(entity_id=item.id,kind='cancelled'))
