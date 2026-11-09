from dataclasses import replace
from sqlalchemy import select, func
from sqlalchemy.orm import sessionmaker
import pytest
from company.queue import Queue
from company.database import WorkItem, WorkReview, Attempt
from test_migrations import migrated_engine, migrate


def test_human_requeue_preserves_history_and_refuses_external(migrated_engine):
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions)
    key=q.submit('review','review',max_attempts=1)
    item=q.claim('worker'); q.finish(key,item.token,success=False)
    for actor,reason in [('', 'r'),('human',''),('human\nforged','r')]:
        with pytest.raises(ValueError):
            q.requeue(key,actor=actor,reason=reason)
    q.requeue(key,actor='julien',reason='cause corrigée')
    with pytest.raises(ValueError):
        q.requeue(key,actor='julien',reason='double')
    with sessions() as db:
        assert db.get(WorkItem,key).state=='PENDING'
        assert db.scalar(select(func.count()).select_from(Attempt))==1
        assert db.scalar(select(WorkReview)).actor=='julien'
    item=q.claim('replacement'); q.finish(key,item.token)
    key=q.submit('external','effect',external=True,max_attempts=1)
    item=q.claim('external'); q.finish(key,item.token,success=False)
    with pytest.raises(ValueError):
        q.requeue(key,actor='julien',reason='never')


def test_static_validation_never_executes_js(tmp_path):
    from company.static_validation import validate
    (tmp_path/'index.html').write_text('<html><title>Demo</title><body><script src="app.js"></script></body></html>')
    (tmp_path/'app.js').write_text('throw new Error("must never run");')
    validate(tmp_path)
    (tmp_path/'index.html').write_text('<html><title>Demo</title><body><script src="https://remote.test/code.js"></script></body></html>')
    with pytest.raises(ValueError,match='URL refused'):
        validate(tmp_path)
    (tmp_path/'index.html').write_text('<body>incomplete')
    with pytest.raises(ValueError,match='required'):
        validate(tmp_path)


def test_sandbox_refuses_missing_cgroup_limits(tmp_path,monkeypatch):
    import subprocess
    from company import sandbox
    monkeypatch.setenv('SANDBOX_WORKDIR',str(tmp_path/'sandbox'))
    calls=[]
    def docker(binary,*args,**kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args,0,b'false false false false',b'')
    monkeypatch.setattr(sandbox,'_docker',docker)
    result=sandbox.execute([], 'python-unittest','sha256:'+'a'*64)
    assert result.state=='ERROR' and 'limites' in result.detail
    assert len(calls)==1 and calls[0][0]=='info'


def test_duplicate_run_id_never_cleans_another_workspace(tmp_path,monkeypatch):
    from company import sandbox
    monkeypatch.setenv('SANDBOX_WORKDIR',str(tmp_path))
    monkeypatch.setattr(sandbox,'resource_limits_supported',lambda d:True)
    calls=[]
    monkeypatch.setattr(sandbox,'_remove',lambda *a:calls.append(a))
    run_id='a'*32
    root=tmp_path/run_id; root.mkdir(); marker=root/'active'; marker.write_text('keep')
    result=sandbox.execute([], 'python-unittest','sha256:'+'a'*64,run_id=run_id)
    assert result.state=='ERROR'
    assert marker.read_text()=='keep' and calls==[]


def test_no_discovered_tests_is_not_a_success(tmp_path):
    import subprocess,sys
    from company.sandbox import PROFILES
    script=PROFILES['python-unittest'].argv[-1].replace('/workspace',str(tmp_path))
    result=subprocess.run([sys.executable,'-I','-B','-c',script],capture_output=True,text=True)
    assert result.returncode==2 and 'Discovered tests: 0' in result.stdout


def test_sandbox_dashboard_read_only(migrated_engine,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from company.dashboard import create_app
    from company.config import Settings
    from company.budget import Budget
    from company import sandbox
    from test_sandbox import generated_task, FAKE_IMAGE
    sessions,task_id=generated_task(migrated_engine,tmp_path,monkeypatch)
    sandbox.approve_execution(sessions,task_id,profile='static-html',image_id=FAKE_IMAGE,
                              actor='julien',reason='fixture reviewed')
    def executor(*args,**kwargs):
        return sandbox.SandboxResult('PASSED',0,10,'SENSITIVE OUTPUT','',16,False)
    sandbox.run_approved(sessions,task_id,images={'static-html':FAKE_IMAGE},executor=executor)
    settings=Settings.load()
    client=TestClient(create_app(sessions,settings,Budget(sessions,settings)))
    response=client.get('/api/sandbox-runs')
    assert response.status_code==200
    assert response.json()[0]['task_id']==task_id
    assert response.json()[0]['state']=='PASSED'
    assert 'SENSITIVE' not in response.text
    assert 'Exécutions isolées' in client.get('/').text
    assert client.post('/api/sandbox-runs').status_code==405


def test_human_review_resets_busy_budget_without_erasing_attempts(migrated_engine):
    from datetime import timedelta
    from company.database import utcnow
    from company.queue import MAX_BUSY_DEFERRALS
    migrate(migrated_engine)
    sessions=sessionmaker(migrated_engine,expire_on_commit=False)
    q=Queue(sessions); key=q.submit('busy-review','fixture')
    for _ in range(MAX_BUSY_DEFERRALS):
        item=q.claim('worker'); assert item is not None
        q.defer_busy(key,item.token)
        with sessions.begin() as db:
            db.get(WorkItem,key).scheduled_at=utcnow()-timedelta(seconds=1)
    with sessions() as db:
        assert db.get(WorkItem,key).state=='WAITING_APPROVAL'
    q.requeue(key,actor='julien',reason='old worker stopped')
    item=q.claim('replacement')
    assert q.defer_busy(key,item.token)=='RETRY_SCHEDULED'
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Attempt))==MAX_BUSY_DEFERRALS+1
