"""Supervision en lecture seule : navigation, contenu hostile, chemins, pagination, absence de mutation."""
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect, select, text
from sqlalchemy.orm import sessionmaker

from company import config, prototyping, sandbox, supervision
from company.approvals import approve_task
from company.budget import Budget
from company.config import Settings
from company.dashboard import create_app
from company.database import Ledger, Run, SandboxRun, Task, WorkItem
from company.queue import Queue
from company.workflow import Board
from test_migrations import migrate, migrated_engine  # noqa: F401

IMAGE = "sha256:" + "a" * 64
HOSTILE_MISSION = '<script>alert("mission")</script> Mission & test'
def shown(response) -> str:
    """Text as the reader sees it (entities decoded); tag checks use the raw HTML instead."""
    return html.unescape(response.text)


REAL_TAG = re.compile(r"<(script|img|svg|iframe|object)\b", re.I)


def build(engine, tmp_path, monkeypatch, *, settings_overrides=None):
    migrate(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = replace(Settings.load(), **{"mock": True, **(settings_overrides or {})})
    monkeypatch.setattr(prototyping, "ROOT", tmp_path)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    budget = Budget(sessions, settings)
    client = TestClient(create_app(sessions, settings, budget))
    return sessions, settings, budget, client


def seed(sessions, settings, budget, tmp_path):
    """MOCK meetings, hostile text, generated prototype, fake sandbox run, queue and ledger states."""
    for _ in range(2):  # reports land under the patched config.ROOT
        Board(sessions, settings, budget).run_once()
    with sessions() as db:
        run_id = db.scalars(select(Run.id).order_by(Run.started_at)).first()
        proto = db.scalars(select(Task).where(Task.category == "prototype").order_by(Task.created_at)).first()
    with sessions.begin() as db:
        run = db.get(Run, run_id)
        run.mission = HOSTILE_MISSION
        data = json.loads(run.result_json)
        data["proposals"][0]["title"] = "<img src=x onerror=alert(1)>Titre"
        data["sources"] = {"openai": [{"title": "<b>source</b>", "url": "javascript:alert(2)",
                                       "snippet": '"><svg onload=alert(3)>'}]}
        run.result_json = json.dumps(data)
    approve_task(sessions, proto.id, actor="julien")
    prototyping.generate_prototype(sessions, settings, budget, proto.id)
    with sessions.begin() as db:  # developer fixture content with active HTML
        task = db.get(Task, proto.id)
        files = [{"path": "index.html", "content": "<html><body><script>alert('generated')</script></body></html>"},
                 {"path": "README.md", "content": "# Lisez-moi"}]
        task.artifact_manifest = json.dumps({"provider": "fixture", "files": files})
    root = tmp_path / "artifacts" / "prototypes" / proto.id
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    prototyping.safe_files(root, files)
    sandbox.approve_execution(sessions, proto.id, profile="static-html", image_id=IMAGE, actor="julien",
                              reason="relu <script>x</script>")

    def fake(*args, **kwargs):  # no Docker, no code executed
        return sandbox.SandboxResult("FAILED", 1, 42, '<script>alert("stdout")</script>\x1b[31m', "", 30, False)
    sandbox.run_approved(sessions, proto.id, images={"static-html": IMAGE}, executor=fake)
    queue = Queue(sessions)
    queue.submit("demo-ok", settings.mission)
    failed = queue.submit("demo-failed", settings.mission)
    with sessions.begin() as db:
        item = db.get(WorkItem, failed)
        item.state, item.attempts = "FAILED", 3
        for state, amount, est, act, ref in (("reserved", "0.0123", "0.0123", None, None),
                                             ("settled", "0.004210", "0.0123", "0.004210", None),
                                             ("unknown", "0.0300", "0.0300", None, None),
                                             ("reconciled", "0.0201", "0.0123", "0.0201", "Relevé <b>nov</b>")):
            db.add(Ledger(run_id=run_id, provider="openai", amount_eur=Decimal(amount), estimated_eur=Decimal(est),
                          actual_eur=Decimal(act) if act else None, state=state, reconciliation_reference=ref))
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "reports" / f"{run_id}.md").write_text("# Rapport\n<script>alert('report')</script>\n")
    return run_id, proto.id, failed


def crawl(client, start="/", limit=120):
    seen, todo, pages = set(), [start], {}
    while todo and len(seen) < limit:
        url = todo.pop(0)
        if url in seen:
            continue
        seen.add(url)
        response = client.get(url)
        pages[url] = response
        if "text/html" in response.headers.get("content-type", ""):
            for href in re.findall(r'href=["\']([^"\']+)', response.text):
                if href.startswith("/") and not href.startswith("//"):
                    todo.append(href.split("#")[0])
    return pages


def fingerprint(engine) -> str:
    """Content hash of every table: any write would change it."""
    digest = hashlib.sha256()
    with engine.connect() as connection:
        for name in sorted(inspect(engine).get_table_names()):
            if name == "alembic_version":
                continue
            rows = connection.execute(text(f"SELECT * FROM {name}")).all()
            digest.update(name.encode())
            for row in sorted(repr(tuple(r)) for r in rows):
                digest.update(row.encode())
    return digest.hexdigest()


# --------------------------------------------------------------------------- tests

def test_every_linked_page_renders_and_escapes_hostile_content(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    seed(sessions, settings, budget, tmp_path)
    pages = crawl(client)
    assert len(pages) > 15
    for url, response in pages.items():
        assert response.status_code == 200, url
        if "text/html" in response.headers["content-type"]:
            assert not REAL_TAG.search(response.text), url
            assert response.headers["content-security-policy"].startswith("default-src 'none'")
            assert "script-src" not in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
    for path in ("/", "/runs", "/tasks", "/work", "/sandbox", "/budget"):
        assert path in pages and "SIMULATION (MOCK)" in pages[path].text


def test_meeting_detail_shows_decision_sources_and_report_safely(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    run_id, _, _ = seed(sessions, settings, budget, tmp_path)
    raw = client.get(f"/runs/{run_id}")
    page = shown(raw)
    assert "&lt;script&gt;alert(&quot;mission&quot;)&lt;/script&gt;" in raw.text and HOSTILE_MISSION in page
    for expected in ("Propositions", "Critiques", "Votes", "Décision", "Sources (non vérifiées)",
                     "Tâches créées", "pas une preuve de demande commerciale"):
        assert expected in page
    stages = re.findall(r"<tr><td>(proposal|critique|vote)</td>", raw.text)
    assert stages == sorted(stages, key=["proposal", "critique", "vote"].index) and stages  # cycle order
    assert 'href="javascript' not in raw.text and "javascript:alert(2)" in page  # URL shown as text only
    report = client.get(f"/runs/{run_id}/report")
    assert report.status_code == 200 and "&lt;script&gt;alert(&#x27;report&#x27;)" in report.text
    download = client.get(f"/runs/{run_id}/report?download=1")
    assert download.headers["content-type"].startswith("text/plain")
    assert "attachment" in download.headers["content-disposition"]


def test_report_route_never_reads_outside_reports(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    run_id, _, _ = seed(sessions, settings, budget, tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    with sessions.begin() as db:  # tampered stored path is ignored
        db.get(Run, run_id).report_path = str(secret)
    assert "TOP-SECRET" not in client.get(f"/runs/{run_id}/report").text
    for attack in ("..%2F..%2Fsecret.txt", "%2e%2e%2fsecret.txt", "..", "abc", run_id + "x", run_id.upper()):
        response = client.get(f"/runs/{attack}/report")
        assert response.status_code == 404 and "TOP-SECRET" not in response.text
    report = tmp_path / "reports" / f"{run_id}.md"
    report.unlink()
    report.symlink_to(secret)
    response = client.get(f"/runs/{run_id}/report")
    assert response.status_code == 404 and "TOP-SECRET" not in response.text


def test_report_is_bounded(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    run_id, _, _ = seed(sessions, settings, budget, tmp_path)
    (tmp_path / "reports" / f"{run_id}.md").write_text("x" * (supervision.REPORT_MAX_BYTES + 50_000))
    page = client.get(f"/runs/{run_id}/report").text
    assert "tronqué" in page and len(page) < supervision.REPORT_MAX_BYTES + 20_000


def test_generated_files_are_text_only_and_index_bound(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    _, task_id, _ = seed(sessions, settings, budget, tmp_path)
    page = client.get(f"/tasks/{task_id}/files/0")
    assert page.status_code == 200 and "&lt;script&gt;alert(&#x27;generated&#x27;)" in page.text
    assert not REAL_TAG.search(page.text)
    download = client.get(f"/tasks/{task_id}/files/0?download=1")
    assert download.headers["content-type"].startswith("text/plain")
    assert download.headers["content-security-policy"].startswith("sandbox")
    assert "attachment" in download.headers["content-disposition"]
    for bad in ("2", "-1", "999", "..%2F..%2Fetc%2Fpasswd", "index.html"):
        assert client.get(f"/tasks/{task_id}/files/{bad}").status_code in (404, 422)


def test_separate_approvals_and_cli_actions_are_shown(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    _, task_id, failed = seed(sessions, settings, budget, tmp_path)
    page = shown(client.get(f"/tasks/{task_id}"))
    assert "Génération" in page and "n'autorise jamais l'exécution" in page
    assert ">consommée<" in page and ">disponible<" not in page  # single-use, already consumed
    assert f"python -m company sandbox approve {task_id} --reviewed" in page
    work = shown(client.get(f"/work/{failed}"))
    assert f"python -m company.worker requeue {failed}" in work
    home = client.get("/").text
    assert "À votre attention" in shown(client.get("/")) and "reconcile-cost" in home and "<button" not in home and "<form" not in home


def test_costs_are_presented_by_certainty(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    seed(sessions, settings, budget, tmp_path)
    page = shown(client.get("/budget"))
    for label in ("Réservation initiale", "Coût calculé depuis les tokens", "Coût inconnu", "Rapproché par un humain",
                  "pas une facture de l'opérateur"):
        assert label in page
    assert "0.030000 €" in page and "Relevé <b>nov</b>" in page
    unknown = client.get("/budget?state=unknown").text
    assert unknown.count("<tr id='l-") == 1 and "0.030000 €</strong>" in unknown
    assert client.get("/budget?state=<script>").status_code == 200  # ignored filter


def test_sandbox_results_are_labelled_untrusted(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    _, task_id, _ = seed(sessions, settings, budget, tmp_path)
    with sessions() as db:
        run = db.scalar(select(SandboxRun).where(SandboxRun.task_id == task_id))
    raw = client.get(f"/sandbox/{run.id}")
    page = shown(raw)
    for expected in ("non fiable", "ni une validation commerciale", IMAGE, run.manifest_sha256,
                     "Validation statique HTML", '<script>alert("stdout")</script>'):
        assert expected in page
    assert not REAL_TAG.search(raw.text)
    assert "\x1b" not in raw.text and "\\x1b" in raw.text  # terminal escape shown, never emitted


def test_read_routes_never_mutate_and_reject_writes(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    run_id, task_id, failed = seed(sessions, settings, budget, tmp_path)
    before = fingerprint(migrated_engine)
    pages = crawl(client)
    for url in ("/api/status", "/api/sandbox-runs", f"/runs/{run_id}/report?download=1"):
        client.get(url)
    assert fingerprint(migrated_engine) == before
    for url in list(pages)[:20] + [f"/tasks/{task_id}", f"/work/{failed}"]:
        for method in ("post", "put", "delete", "patch"):
            assert getattr(client, method)(url.split("?")[0]).status_code in (404, 405)
    assert fingerprint(migrated_engine) == before


def test_empty_database_and_unknown_items(migrated_engine, tmp_path, monkeypatch):
    _, _, _, client = build(migrated_engine, tmp_path, monkeypatch)
    for path, message in (("/", "Rien n'attend votre intervention"), ("/runs", "Aucune réunion"),
                          ("/tasks", "Aucune tâche"), ("/work", "Aucun travail"),
                          ("/sandbox", "Aucune exécution isolée"), ("/budget", "Aucune écriture")):
        response = client.get(path)
        assert response.status_code == 200 and message in shown(response)
    for path in ("/runs/" + "0" * 32, "/tasks/" + "0" * 32, "/work/" + "0" * 32, "/sandbox/" + "0" * 32,
                 "/tasks/not-an-id", "/runs/%3Cscript%3E", "/runs/..%2F..%2Fetc%2Fpasswd/report", "/nowhere"):
        response = client.get(path)
        assert response.status_code == 404 and "Introuvable" in response.text and "<script" not in response.text


def test_pagination_bounds(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    with sessions.begin() as db:
        for n in range(supervision.PAGE_SIZE + 5):
            db.add(Run(mission=f"mission {n}", mode="mock", status="completed"))
    first, second = client.get("/runs").text, client.get("/runs?page=2").text
    assert "Page 1 / 2" in first and "Suivant" in first and "Page 2 / 2" in second
    assert first.count("<tr>") - 1 == supervision.PAGE_SIZE and second.count("<tr>") - 1 == 5
    assert "Aucune réunion" in client.get("/runs?page=50").text
    for bad in ("0", "-3", "abc", str(supervision.MAX_PAGE + 1)):
        assert client.get(f"/runs?page={bad}").status_code == 422


def test_list_pages_do_not_query_per_item(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    seed(sessions, settings, budget, tmp_path)
    counter = {"n": 0}

    def count(*args, **kwargs):
        counter["n"] += 1
    event.listen(migrated_engine, "before_cursor_execute", count)
    try:
        def queries(path):
            counter["n"] = 0
            assert client.get(path).status_code == 200
            return counter["n"]
        baseline = {p: queries(p) for p in ("/", "/runs", "/tasks", "/work", "/sandbox", "/budget")}
        with sessions.begin() as db:
            for n in range(20):
                run = Run(mission=f"m{n}", mode="mock", status="completed")
                db.add(run)
                db.flush()
                db.add(Task(run_id=run.id, title=f"t{n}", category="validation"))
                db.add(Ledger(run_id=run.id, provider="gemini", amount_eur=Decimal("0.01"), state="settled"))
        grown = {p: queries(p) for p in baseline}
    finally:
        event.remove(migrated_engine, "before_cursor_execute", count)
    assert grown == baseline and max(grown.values()) <= 15


def test_database_failure_shows_neutral_error_without_secrets(migrated_engine, tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError
    sessions, settings, budget, client = build(
        migrated_engine, tmp_path, monkeypatch,
        settings_overrides={"keys": {"openai": "sk-CANARY-KEY"}, "db_url": "postgresql://u:CANARY-PW@h/db"})

    class Broken:
        def __call__(self, *args, **kwargs):
            raise OperationalError("SELECT secret", {}, Exception("password=CANARY-PW"))
    broken = TestClient(create_app(Broken(), settings, budget), raise_server_exceptions=False)
    response = broken.get("/runs")
    assert response.status_code == 503 and "Base de données indisponible" in response.text
    assert "CANARY" not in response.text and "SELECT" not in response.text
    for path in ("/", "/budget", "/api/status"):
        assert "CANARY" not in client.get(path).text


def test_real_mode_is_flagged(migrated_engine, tmp_path, monkeypatch):
    _, _, _, client = build(migrated_engine, tmp_path, monkeypatch, settings_overrides={"mock": False})
    page = client.get("/").text
    assert "API RÉELLES" in page and "SIMULATION (MOCK)" not in page


@pytest.mark.parametrize("data", [
    {"proposals": 42}, {"votes": True}, {"errors": 42},
    {"sources": {"openai": {"unexpected": "shape"}}},
    {"sources": {"openai": 42}},
])
def test_malformed_meeting_fields_do_not_break_supervision(migrated_engine, tmp_path, monkeypatch, data):
    sessions, _, _, client = build(migrated_engine, tmp_path, monkeypatch)
    with sessions.begin() as db:
        run = Run(mission="Malformed imported report", mode="mock", status="failed", result_json=json.dumps(data))
        db.add(run)
        db.flush()
        run_id = run.id
    before = fingerprint(migrated_engine)
    assert client.get(f"/runs/{run_id}").status_code == 200
    assert fingerprint(migrated_engine) == before


def test_invalid_sandbox_summary_does_not_break_json_api(migrated_engine, tmp_path, monkeypatch):
    sessions, settings, budget, client = build(migrated_engine, tmp_path, monkeypatch)
    seed(sessions, settings, budget, tmp_path)
    with sessions.begin() as db:
        db.scalar(select(SandboxRun)).summary_json = "{invalid"
    response = client.get("/api/sandbox-runs")
    assert response.status_code == 200
    assert response.json()[0]["summary"] is None


def test_measure_command_can_be_copied_without_shell_pipe():
    from shlex import split
    task = Task(id="a" * 32, category="validation", status="approved")
    markup = supervision.task_action(task, True, False, False)
    command = html.unescape(re.search(r"<code>(.*?)</code>", markup).group(1))
    assert "|" not in command
    args = split(command)
    assert args[args.index("--success") + 1] in ("0", "1")


def test_html_demo_suggests_static_validation_not_python_tests():
    task = Task(id="a" * 32, category="prototype", status="awaiting_evaluation",
                artifact_manifest=json.dumps({"files": [{"path": "index.html", "content": "<html></html>"}]}))
    markup = html.unescape(supervision.task_action(task, True, False, True))
    assert "--profile static-html" in markup
    assert "SANDBOX_PYTHON_IMAGE_ID" in markup
