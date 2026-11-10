import json
import time
import pytest
from sqlalchemy import select
from company import sandbox
from company.database import SandboxRun, SandboxCleanup, SandboxEvidence, ExecutionApproval
from company.sandbox_summary import parse_summary,PREFIX
from test_migrations import migrated_engine
from test_sandbox import generated_task, FAKE_IMAGE
from test_sandbox_hardening import _orphan


def setup_cleanup(engine,tmp_path,monkeypatch):
    sessions,task=generated_task(engine,tmp_path,monkeypatch)
    run_id=_orphan(sessions,task,monkeypatch,tmp_path)
    root=sandbox.secure_base()/run_id; root.mkdir(); (root/'keep').write_text('evidence')
    name=sandbox.NAME_PREFIX+run_id
    containers={name:{'deadline':1,'state':'exited'}}
    monkeypatch.setattr(sandbox,'_containers',lambda d:dict(containers))
    return sessions,run_id,root,name,containers


def test_failed_removal_preserves_files_state_and_audit(migrated_engine,tmp_path,monkeypatch):
    sessions,run_id,root,name,containers=setup_cleanup(migrated_engine,tmp_path,monkeypatch)
    monkeypatch.setattr(sandbox,'_remove',lambda *args:None)
    report=sandbox.cleanup_run(sessions,run_id,actor='julien',reason='recovery')
    assert report['outcome']=='cleanup_failed' and report['containers_removed']==[]
    assert (root/'keep').read_text()=='evidence'
    with sessions() as db:
        assert db.get(SandboxRun,run_id).state=='RUNNING'
        audit=db.scalar(select(SandboxCleanup))
        assert audit.outcome=='cleanup_failed' and not audit.container_removed


def test_crash_after_remove_retains_intent_and_recovers(migrated_engine,tmp_path,monkeypatch):
    sessions,run_id,root,name,containers=setup_cleanup(migrated_engine,tmp_path,monkeypatch)
    def crash(docker,n):
        containers.pop(n); raise SystemExit(17)
    monkeypatch.setattr(sandbox,'_remove',crash)
    with pytest.raises(SystemExit):
        sandbox.cleanup_run(sessions,run_id,actor='julien',reason='crash')
    with sessions() as db:
        assert db.scalar(select(SandboxCleanup)).outcome=='started'
        assert db.get(SandboxRun,run_id).state=='RUNNING'
    # Make the crashed supervisor's SQL deadline expire; no container remains.
    report=sandbox.cleanup_run(sessions,run_id,actor='julien',reason='resume',now=time.time()+10000)
    assert report['outcome']=='interrupted' and not root.exists()
    with sessions() as db:
        assert len(db.scalars(select(SandboxCleanup)).all())==2


def test_lost_daemon_does_not_mean_absent(migrated_engine,tmp_path,monkeypatch):
    sessions,run_id,root,name,containers=setup_cleanup(migrated_engine,tmp_path,monkeypatch)
    calls=[]
    def listing(d):
        calls.append(d)
        if len(calls)>1: raise sandbox.SandboxError('offline')
        return containers
    monkeypatch.setattr(sandbox,'_containers',listing)
    monkeypatch.setattr(sandbox,'_remove',lambda *a:None)
    result=sandbox.cleanup_run(sessions,run_id,actor='julien',reason='offline')
    assert result['outcome']=='cleanup_failed' and root.exists()


def test_live_supervisor_excludes_cleanup(migrated_engine,tmp_path,monkeypatch):
    sessions,run_id,root,*_=setup_cleanup(migrated_engine,tmp_path,monkeypatch)
    with sandbox.lifecycle_lock(run_id):
        with pytest.raises(sandbox.SandboxRefused,match='Superviseur'):
            sandbox.cleanup_run(sessions,run_id,actor='julien',reason='race',now=time.time()+10000)
    assert root.exists()


def test_result_evidence_and_legacy_approval(migrated_engine,tmp_path,monkeypatch):
    sessions,task=generated_task(migrated_engine,tmp_path,monkeypatch)
    args=dict(profile='python-unittest',image_id=FAKE_IMAGE,actor='julien',reason='review')
    old=sandbox.approve_execution(sessions,task,**args)
    with sessions.begin() as db: db.get(ExecutionApproval,old).validator_version=None
    with pytest.raises(sandbox.SandboxRefused,match='Validateur'):
        sandbox.run_approved(sessions,task,images={'python-unittest':FAKE_IMAGE},executor=lambda *a,**k:None)
    sandbox.approve_execution(sessions,task,**args)
    def executor(*a,**kwargs):
        with sessions.begin() as db:
            r=db.get(SandboxRun,kwargs['run_id']); r.state='INTERRUPTED'; r.stdout='previous evidence'
            r.output_bytes=1000; r.truncated=True
            r.summary_json='{"kind":"static","structure_valid":true}'
        return sandbox.SandboxResult('FAILED',1,10,'late evidence','',13,False)
    run=sandbox.run_approved(sessions,task,images={'python-unittest':FAKE_IMAGE},executor=executor)
    assert run.state=='INTERRUPTED' and run.stdout=='previous evidence'
    assert run.output_bytes==1000 and run.truncated
    assert json.loads(run.summary_json)['kind']=='static'
    with sessions() as db:
        evidence=db.scalar(select(SandboxEvidence))
        assert json.loads(evidence.outcome_json)['stdout']=='late evidence'


def test_summary_schema_is_bounded_and_observational():
    counts=dict(discovered=3,run=3,passed=1,failed=1,errors=0,skipped=1,expected_failures=0,unexpected_successes=0)
    line=PREFIX+json.dumps({'kind':'unit','counts':counts})
    assert parse_summary(line,'python-unittest')['counts']==counts
    assert parse_summary(line+'\n'+line,'python-unittest') is None
    assert parse_summary(line,'python-unittest',truncated=True) is None
    counts['passed']=True
    assert parse_summary(PREFIX+json.dumps({'kind':'unit','counts':counts}),'python-unittest') is None
    assert parse_summary(PREFIX+'{"kind":"static","structure_valid":true}','static-html')['kind']=='static'
    assert parse_summary(PREFIX+'[]','static-html') is None
    assert parse_summary(PREFIX+'{"kind":"static","structure_valid":1}','static-html') is None


def test_fixed_runner_reports_all_outcomes(tmp_path):
    import subprocess,sys
    from pathlib import Path
    from company.sandbox import PROFILES
    (tmp_path/'test_fixture.py').write_text('''import unittest
class Cases(unittest.TestCase):
    def test_pass(self): pass
    def test_fail(self): self.fail('expected fixture failure')
    def test_error(self): raise ValueError('expected fixture error')
    @unittest.skip('fixture')
    def test_skip(self): pass
    @unittest.expectedFailure
    def test_expected(self): self.fail('fixture')
    @unittest.expectedFailure
    def test_unexpected(self): pass
''')
    # App-owned runner against a developer-authored fixture; not an isolation test.
    script=PROFILES['python-unittest'].argv[-1].replace('/workspace',str(tmp_path))
    result=subprocess.run([sys.executable,'-I','-B','-c',script],capture_output=True,text=True)
    assert result.returncode==1
    counts=parse_summary(result.stderr,'python-unittest')['counts']
    assert counts==dict(discovered=6,run=6,passed=1,failed=1,errors=1,skipped=1,expected_failures=1,unexpected_successes=1)


def test_report_does_not_override_verdict_or_approval(migrated_engine,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from company.dashboard import create_app
    from company.config import Settings
    from company.budget import Budget
    sessions,task=generated_task(migrated_engine,tmp_path,monkeypatch)
    sandbox.approve_execution(sessions,task,profile='static-html',image_id=FAKE_IMAGE,actor='julien',reason='review')
    def executor(*args,**kwargs):
        return sandbox.SandboxResult('FAILED',1,10,'',PREFIX+'{"kind":"static","structure_valid":true}',40,False)
    run=sandbox.run_approved(sessions,task,images={'static-html':FAKE_IMAGE},executor=executor)
    assert run.state=='FAILED' and json.loads(run.summary_json)['kind']=='static'
    client=TestClient(create_app(sessions,Settings.load(),Budget(sessions,Settings.load())))
    row=client.get('/api/sandbox-runs').json()[0]
    assert row['state']=='FAILED' and row['validator_version']==sandbox.validator_version('static-html')
    assert row['manifest_sha256']==run.manifest_sha256
    assert 'Résumé déclaré' in client.get('/').text
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.run_approved(sessions,task,images={'static-html':FAKE_IMAGE},executor=executor)


def _lock_child(root,run_id,connection):
    import os,time
    os.environ['SANDBOX_WORKDIR']=root
    with sandbox.lifecycle_lock(run_id):
        connection.send(True)
        time.sleep(30)


def test_lock_released_after_supervisor_process_dies(tmp_path,monkeypatch):
    import multiprocessing
    monkeypatch.setenv('SANDBOX_WORKDIR',str(tmp_path))
    context=multiprocessing.get_context('spawn')
    parent,child=context.Pipe()
    run_id='d'*32
    process=context.Process(target=_lock_child,args=(str(tmp_path),run_id,child))
    process.start()
    try:
        assert parent.poll(10) and parent.recv()
        with pytest.raises(sandbox.SandboxRefused):
            with sandbox.lifecycle_lock(run_id): pass
    finally:
        process.terminate(); process.join(10)
        if process.is_alive(): process.kill(); process.join()
        parent.close(); child.close()
    with sandbox.lifecycle_lock(run_id): pass


def test_successful_exit_without_valid_report_is_not_passed():
    assert sandbox.execution_verdict(0,'','python-unittest')=='ERROR'
    assert sandbox.execution_verdict(0,PREFIX+'{"kind":"static","structure_valid":true}','static-html')=='PASSED'
    assert sandbox.execution_verdict(1,PREFIX+'{"kind":"static","structure_valid":true}','static-html')=='FAILED'
    assert sandbox.execution_verdict(0,PREFIX+'{"kind":"static","structure_valid":true}','static-html',truncated=True)=='ERROR'
