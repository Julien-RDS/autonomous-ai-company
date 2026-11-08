"""Durable MOCK worker. Real provider execution requires a separate authorization flow."""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import logging
import threading
import time
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from langgraph.checkpoint.postgres import PostgresSaver
from .config import Settings
from .database import session_factory
from .budget import Budget
from .queue import Queue, LostLease, Busy
from .workflow import Board

@contextmanager
def checkpoints(url, thread_id=None):
    parsed=make_url(url)
    if parsed.get_backend_name() != 'postgresql':
        raise ValueError('Worker durable: PostgreSQL requis')
    dsn=parsed.set(drivername='postgresql').render_as_string(hide_password=False)
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.conn.execute('SELECT pg_advisory_lock(7109244)')
        try:
            saver.setup()
        finally:
            saver.conn.execute('SELECT pg_advisory_unlock(7109244)')
        locked = False
        try:
            if thread_id is not None:
                locked = saver.conn.execute(
                    'SELECT pg_try_advisory_lock(hashtextextended(%s, 7109245))', (thread_id,)
                ).fetchone()['pg_try_advisory_lock']
                if not locked:
                    raise Busy('Workflow encore détenu par un autre processus')
            yield saver
        finally:
            if locked:
                saver.conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s, 7109245))', (thread_id,))


def work_once(sessions, settings, owner):
    if not settings.mock:
        raise ValueError('Worker réel désactivé: autorisation et rapprochement externes requis')
    queue=Queue(sessions)
    item=queue.claim(owner)
    if item is None:
        return False
    stopped=threading.Event()
    lost=threading.Event()
    def heartbeat():
        while not stopped.wait(queue.lease_seconds/3):
            try:
                queue.heartbeat(item.id,item.token)
            except Exception:
                lost.set(); return
    thread=threading.Thread(target=heartbeat,daemon=True); thread.start()
    try:
        if item.kind == 'prototype':
            from .prototyping import generate_prototype
            generate_prototype(sessions, settings, Budget(sessions, settings), item.task_id,
                               lease=(item.id, item.token))
        else:
            with checkpoints(settings.db_url, item.run_id) as saver:
                Board(sessions,settings,Budget(sessions,settings)).run_once(
                    item.run_id,saver,lease=(item.id,item.token))
        if lost.is_set():
            raise LostLease('Heartbeat perdu')
        queue.finish(item.id,item.token)
    except LostLease:
        logging.warning('worker_lost_lease work_id=%s',item.id)
    except Busy:
        # A previous owner whose lease expired is still running: wait, do not fail.
        logging.warning('worker_busy work_id=%s',item.id)
        try:
            queue.defer_busy(item.id,item.token)
        except LostLease:
            pass
    except Exception as exc:
        logging.warning('worker_failed work_id=%s error=%s',item.id,type(exc).__name__)
        try:
            queue.finish(item.id,item.token,success=False)
        except LostLease:
            pass
    finally:
        stopped.set(); thread.join()
    return True


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='action',required=True)
    enqueue=sub.add_parser('enqueue'); enqueue.add_argument('key')
    prototype=sub.add_parser('enqueue-prototype'); prototype.add_argument('task_id')
    worker=sub.add_parser('run'); worker.add_argument('--once',action='store_true')
    worker.add_argument('--owner',default='local-worker')
    args=parser.parse_args()
    settings=Settings.load()
    if not settings.mock:
        parser.error('MOCK_MODE=true requis pour le worker de ce lot')
    sessions=session_factory(settings.db_url)
    if args.action=='enqueue':
        print(Queue(sessions).submit(args.key,settings.mission))
    elif args.action=='enqueue-prototype':
        print(Queue(sessions).submit_prototype(args.task_id))
    else:
        failures = 0
        while True:
            try:
                worked=work_once(sessions,settings,args.owner)
                failures = 0
            except OperationalError:
                failures += 1
                logging.warning('database_unavailable attempt=%s', failures)
                if failures >= 5:
                    raise SystemExit('PostgreSQL indisponible après 5 tentatives') from None
                time.sleep(min(30, 2 ** failures))
                continue
            if args.once:
                break
            if not worked:
                time.sleep(1)

if __name__=='__main__':
    main()
