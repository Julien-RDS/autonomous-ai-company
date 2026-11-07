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


@pytest.mark.parametrize('value', ['-1', '0', 'NaN', 'Infinity'])
def test_invalid_reservation_rejected(tmp_path, monkeypatch, value):
    _, _, budget = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        budget.reserve('unused', 'openai', Decimal(value))


def test_settlement_cannot_release_budget_twice(tmp_path, monkeypatch):
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    with sessions.begin() as db:
        run = Run(mode='real', mission='test')
        db.add(run)
        db.flush()
        run_id = run.id
    reservation = budget.reserve(run_id, 'openai', Decimal('0.08'))
    for value in ['-1', 'NaN', 'Infinity']:
        with pytest.raises(ValueError):
            budget.settle(reservation, Decimal(value))
        assert budget.totals(run_id) == Decimal('0.08')
    budget.settle(reservation, None, error=True)
    with pytest.raises(ValueError):
        budget.settle(reservation, Decimal('0'))
    assert budget.totals(run_id) == Decimal('0.08')


def test_concurrent_reservations_respect_monthly_limit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    settings, sessions, _ = setup(tmp_path, monkeypatch)
    budget = Budget(sessions, replace(settings, monthly_limit=Decimal('0.1')))
    with sessions.begin() as db:
        runs = [Run(mode='real', mission='test') for _ in range(2)]
        db.add_all(runs)
        db.flush()
        ids = [r.id for r in runs]
    barrier = Barrier(2)
    def reserve(run_id):
        barrier.wait()
        try:
            budget.reserve(run_id, 'openai', Decimal('0.08'))
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, ids)) == [False, True]
    assert budget.totals() == Decimal('0.08')


def test_provider_failure_retains_reservation_without_secret(tmp_path, monkeypatch, caplog):
    from company.agents import AgentService, ProviderError
    from company.database import Ledger
    settings, sessions, _ = setup(tmp_path, monkeypatch)
    settings = replace(settings, mock=False, models={'openai': 'test'},
                       prices={'openai': (Decimal('1'), Decimal('1'))})
    budget = Budget(sessions, settings)
    service = AgentService(sessions, settings, budget)
    with sessions.begin() as db:
        run = Run(mode='real', mission='test')
        db.add(run)
        db.flush()
        run_id = run.id
    calls = []
    def fail(*args):
        calls.append(args)
        raise TimeoutError('credential-SENSITIVE https://example.test?key=SENSITIVE')
    monkeypatch.setattr(service.provider, 'generate', fail)
    with pytest.raises(ProviderError) as caught:
        service.call('openai', 'proposal', 'hello', run_id)
    assert len(calls) == 1
    assert 'SENSITIVE' not in str(caught.value) + caplog.text
    with sessions() as db:
        msg = db.scalar(select(Message))
        ledger = db.scalar(select(Ledger))
        assert not msg.success and 'SENSITIVE' not in msg.text
        assert ledger.state == 'unknown' and ledger.amount_eur > 0


def test_workflow_failure_does_not_leak_sdk_errors(tmp_path, monkeypatch):
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    board = Board(sessions, settings, budget)
    def fail(*args):
        raise RuntimeError('SENSITIVE')
    monkeypatch.setattr(board.agents, 'call', fail)
    with pytest.raises(RuntimeError) as caught:
        board.run_once()
    assert 'SENSITIVE' not in str(caught.value)
    with sessions() as db:
        assert db.scalar(select(Run)).status == 'failed'
        assert not db.scalars(select(Task)).all()


def test_demo_never_contacts_network(tmp_path, monkeypatch):
    import socket
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    def denied(*args, **kwargs):
        pytest.fail('network used in simulation')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    _, result = Board(sessions, settings, budget).run_once()
    assert result['decision']['votes'] == 2


def test_prototype_requires_approval_before_provider_call(tmp_path, monkeypatch):
    from company.prototyping import generate_prototype
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    run_id, _ = Board(sessions, settings, budget).run_once()
    with sessions() as db:
        task = db.scalar(select(Task).where(Task.category == 'prototype'))
        task_id = task.id
    with pytest.raises(ValueError, match='approuvée'):
        generate_prototype(sessions, settings, budget, task_id)
    with sessions() as db:
        assert len(db.scalars(select(Message)).all()) == 9


def test_prototype_rejects_symlink_escape(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        safe_files(root, [{'path': 'linked/secret.py', 'content': 'x'}])
    assert not (outside / 'secret.py').exists()


@pytest.mark.parametrize('provider', ['openai', 'anthropic', 'gemini'])
def test_sdk_timeouts_and_no_automatic_retry(monkeypatch, provider):
    import sys
    from types import SimpleNamespace
    from company.agents import ProviderClient
    import google
    import google.genai
    settings = SimpleNamespace(mock=False, models={provider: 'test'},
                               keys={provider: 'not-a-real-key'}, max_output_tokens=500)
    options = {}
    class Client:
        def __init__(self, **kwargs):
            options.update(kwargs)
            self.responses = self.messages = self.models = self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def create(self, **kwargs):
            return SimpleNamespace(output_text='ok', content=[SimpleNamespace(type='text', text='ok')],
                                   usage=SimpleNamespace(input_tokens=10, output_tokens=2))
        def generate_content(self, **kwargs):
            return SimpleNamespace(text='ok', usage_metadata=SimpleNamespace(
                prompt_token_count=10, total_token_count=12))
    if provider == 'gemini':
        monkeypatch.setattr(google.genai, 'Client', Client)
    else:
        name = 'OpenAI' if provider == 'openai' else 'Anthropic'
        monkeypatch.setitem(sys.modules, provider, SimpleNamespace(**{name: Client}))
    result = ProviderClient(settings).generate(provider, 'proposal', 'test')
    assert result.text == 'ok' and result.input_tokens == 10 and result.output_tokens == 2
    if provider == 'gemini':
        assert options['http_options'].timeout == 45000
        assert options['http_options'].retry_options.attempts == 1
    else:
        assert options['timeout'] == 45 and options['max_retries'] == 0


def test_report_failure_leaves_no_pending_tasks(tmp_path, monkeypatch):
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    board = Board(sessions, settings, budget)
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(board, 'create_report', fail)
    with pytest.raises(OSError):
        board.run_once()
    with sessions() as db:
        assert db.scalar(select(Run)).status == 'failed'
        assert not db.scalars(select(Task)).all()


def test_offline_fallback_completes_meeting(tmp_path, monkeypatch):
    import company.workflow as workflow
    from company.minigraph import StateGraph
    monkeypatch.setattr(workflow, 'StateGraph', StateGraph)
    settings, sessions, budget = setup(tmp_path, monkeypatch)
    _, result = Board(sessions, settings, budget).run_once()
    assert result['decision']['votes'] == 2
