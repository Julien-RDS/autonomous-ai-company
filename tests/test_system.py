from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from company.budget import Budget, BudgetExceeded
from company.config import Settings
from company.database import Message, Run, Task, session_factory
from company.prototyping import safe_files
from company.routing import rank_providers
from company.workflow import Board, parse_object


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_MODE", "true")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/demo.db")
    monkeypatch.setenv("MONTHLY_BUDGET_EUR", "100")
    monkeypatch.setenv("MAX_RUN_BUDGET_EUR", "3")
    settings = Settings.load()
    sessions = session_factory(settings.db_url)
    return settings, sessions, Budget(sessions, settings)


def test_mock_meeting_has_votes_and_backlog(tmp_path, monkeypatch):
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    run_id, result = Board(sessions, settings, budget).run_once()
    assert result["decision"]["proposal"]["id"] == "anthropic"
    assert result["decision"]["votes"] == 2
    assert len(result["proposals"]) == 3
    with sessions() as db:
        run = db.get(Run, run_id)
        assert run.status == "completed" and run.mode == "mock"
        assert len(db.scalars(select(Message).where(Message.run_id == run_id)).all()) == 9
        tasks = db.scalars(select(Task).where(Task.run_id == run_id)).all()
        assert {t.category for t in tasks} == {"validation", "prototype"}
        assert all(t.status == "pending_review" for t in tasks)
        assert Path(run.report_path).exists()
    assert budget.totals() == Decimal("0")


def test_budget_reserves_then_settles(tmp_path, monkeypatch):
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    limits = replace(settings, monthly_limit=Decimal("0.1"), run_limit=Decimal("0.1"))
    budget = Budget(sessions, limits)
    with sessions.begin() as db:
        run = Run(mode="real", mission="test")
        db.add(run)
        db.flush()
        run_id = run.id
    item = budget.reserve(run_id, "openai", Decimal("0.08"))
    with pytest.raises(BudgetExceeded):
        budget.reserve(run_id, "openai", Decimal("0.04"))
    budget.settle(item, Decimal("0.03"))
    assert budget.totals(run_id) == Decimal("0.03")


def test_prototype_path_restrictions(tmp_path):
    with pytest.raises(ValueError):
        safe_files(tmp_path / "root", [{"path": "../secret.py", "content": "hi"}])
    with pytest.raises(ValueError):
        safe_files(tmp_path / "root", [{"path": "run.sh", "content": "hi"}])
    written = safe_files(tmp_path / "root", [{"path": "demo/index.html", "content": "<h1>Ok</h1>"}])
    assert written[0].read_text() == "<h1>Ok</h1>"


def test_parse_json_markdown():
    assert parse_object('```json\n{"hello": 42}\n```') == {"hello": 42}


def test_router_does_not_invent_quality_without_labels(tmp_path, monkeypatch):
    _, sessions, _ = setup(tmp_path, monkeypatch)
    ranking = rank_providers(sessions)
    assert len(ranking) == 3
    assert all(row["success_rate"] is None and row["samples"] == 0 for row in ranking)
