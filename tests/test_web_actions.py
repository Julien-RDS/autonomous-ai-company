"""Web action security regression tests: disabled by default, origin, CSRF, permissions, separation."""
from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from company.budget import Budget
from company.config import Settings
from company.dashboard import create_app
from company.database import Approval, ExecutionApproval, Ledger, Run, Task, WorkItem
from company.queue import Queue
from test_migrations import migrate, migrated_engine  # noqa: F401

REPO_HOST = {"origin": "http://testserver"}
TOKEN = "T" * 48
SECRET = "S" * 48


def build(engine, monkeypatch, *, permissions="approve"):
    migrate(engine)
    from sqlalchemy.orm import sessionmaker
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = replace(Settings.load(), mock=True)
    monkeypatch.setenv("AAC_WEB_ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("AAC_WEB_SESSION_SECRET", SECRET)
    monkeypatch.setenv("AAC_WEB_PERMISSIONS", permissions)
    return sessions, TestClient(create_app(sessions, settings, Budget(sessions, settings)))


def csrf(client):
    response = client.get("/actions")
    assert response.status_code == 200
    match = re.search(r'name="csrf" value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def login(client):
    result = client.post("/actions/login", data={"token": TOKEN}, headers=REPO_HOST,
                         follow_redirects=False)
    assert result.status_code == 303
    assert result.cookies.get("aac_web_session")
    return csrf(client)


def test_disabled_by_default(migrated_engine, monkeypatch):
    monkeypatch.delenv("AAC_WEB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("AAC_WEB_SESSION_SECRET", raising=False)
    monkeypatch.delenv("AAC_WEB_PERMISSIONS", raising=False)
    migrate(migrated_engine)
    from sqlalchemy.orm import sessionmaker
    sessions = sessionmaker(migrated_engine)
    settings = replace(Settings.load(), mock=True)
    client = TestClient(create_app(sessions, settings, Budget(sessions, settings)))
    response = client.get("/actions")
    assert response.status_code == 200
    assert "Actions désactivées" in response.text
    assert "<form" not in response.text
    assert client.post("/actions/approve", data={}, headers=REPO_HOST).status_code == 404


def test_login_requires_origin_and_credentials(migrated_engine, monkeypatch):
    _, client = build(migrated_engine, monkeypatch)
    assert client.post("/actions/login", data={"token": TOKEN}).status_code == 403
    assert client.post("/actions/login", data={"token": "bad"}, headers=REPO_HOST).status_code == 403
    assert client.post("/actions/login", data={"token": TOKEN},
                       headers={"origin": "http://evil.example"}).status_code == 403
    token = login(client)
    assert len(token) >= 32
    assert client.get("/actions").status_code == 200
    assert '<form action="/actions/approve"' in client.get("/actions").text
    assert '<form action="/actions/sandbox_run"' not in client.get("/actions").text


def test_csrf_and_permissions_guard_state(migrated_engine, monkeypatch):
    sessions, client = build(migrated_engine, monkeypatch)
    with sessions.begin() as db:
        run = Run(mission="M", mode="mock", status="completed")
        db.add(run); db.flush()
        task = Task(run_id=run.id, title="P", category="prototype", status="pending_review")
        db.add(task); db.flush()
        identifier = task.id
    # No session, no action.
    assert client.post("/actions/approve", data={"id": identifier, "csrf": "x"}, headers=REPO_HOST).status_code == 401
    token = login(client)
    assert client.post("/actions/approve", data={"id": identifier, "csrf": "bad"}, headers=REPO_HOST).status_code == 403
    assert client.post("/actions/approve", data={"id": identifier, "csrf": token},
                       headers={"origin": "http://evil.example"}).status_code == 403
    assert client.post("/actions/sandbox_run", data={"id": identifier, "csrf": token, "confirmed": "yes"},
                       headers=REPO_HOST).status_code == 403
    with sessions() as db:
        assert db.get(Task, identifier).status == "pending_review"
    result = client.post("/actions/approve", data={"id": identifier, "csrf": token},
                         headers=REPO_HOST, follow_redirects=False)
    assert result.status_code == 303
    with sessions() as db:
        assert db.get(Task, identifier).status == "approved"
        assert db.scalar(select(Approval).where(Approval.task_id == identifier)) is not None
        assert db.scalar(select(ExecutionApproval).where(ExecutionApproval.task_id == identifier)) is None


def test_invalid_payload_and_session_tampering(migrated_engine, monkeypatch):
    _, client = build(migrated_engine, monkeypatch)
    token = login(client)
    assert client.post("/actions/approve", data={"id": "invalid", "csrf": token},
                       headers=REPO_HOST).status_code == 400
    assert client.post("/actions/approve", content=b"csrf=a&csrf=b&id=a",
                       headers={**REPO_HOST, "content-type": "application/x-www-form-urlencoded"}).status_code == 400
    client.cookies.set("aac_web_session", "not.a.valid.session")
    assert client.post("/actions/approve", data={"id": "0"*32, "csrf": token},
                       headers=REPO_HOST).status_code == 401


def test_requeue_and_reconcile_are_separate_permissions(migrated_engine, monkeypatch):
    sessions, client = build(migrated_engine, monkeypatch, permissions="requeue,reconcile")
    token = login(client)
    queue = Queue(sessions)
    wid = queue.submit("action-requeue", "Mission local")
    with sessions.begin() as db:
        item = db.get(WorkItem, wid)
        item.state = "FAILED"
        run_id = item.run_id
        ledger = Ledger(run_id=run_id, provider="openai", state="unknown",
                        amount_eur=Decimal("0.0123"), estimated_eur=Decimal("0.0123"))
        db.add(ledger); db.flush()
        lid = ledger.id
    assert client.post("/actions/approve", data={"id": "0"*32, "csrf": token},
                       headers=REPO_HOST).status_code == 403
    assert client.post("/actions/requeue", data={"id": wid, "csrf": token, "reason": "revue manuelle"},
                       headers=REPO_HOST, follow_redirects=False).status_code == 303
    assert client.post("/actions/reconcile", data={"id": lid, "csrf": token,
                      "amount": "0.020100", "reference": "Relevé vérifié"},
                      headers=REPO_HOST, follow_redirects=False).status_code == 303
    with sessions() as db:
        assert db.get(WorkItem, wid).state == "PENDING"
        entry = db.get(Ledger, lid)
        assert entry.state == "reconciled"
        assert entry.amount_eur == Decimal("0.020100")
    assert client.post("/actions/reconcile", data={"id": lid, "csrf": token,
                      "amount": "0.5", "reference": "répétition incompatible"},
                      headers=REPO_HOST).status_code == 409


def test_logout_invalidates_browser_cookie(migrated_engine, monkeypatch):
    _, client = build(migrated_engine, monkeypatch)
    token = login(client)
    result = client.post("/actions/logout", data={"csrf": token},
                         headers=REPO_HOST, follow_redirects=False)
    assert result.status_code == 303
    assert client.get("/actions").status_code == 200
    assert "Connexion administrateur" in client.get("/actions").text
