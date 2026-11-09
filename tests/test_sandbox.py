"""Lot 3 : approbation d'exécution dédiée et isolation Docker.

Tous les codes exécutés dans Docker ici sont des FIXTURES ÉCRITES PAR LES
DÉVELOPPEURS (chaînes ci-dessous). Aucun prototype produit par une IA n'est lancé.
Les tests Docker exigent TEST_SANDBOX_IMAGE : une image locale jetable contenant
`python` (voir docs/LOT_3_SANDBOX.md). Ils sont ignorés sans elle.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import subprocess
import time
from argparse import Namespace
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from company import prototyping, sandbox
from company.approvals import approve_task
from company.budget import Budget
from company.config import Settings
from company.database import AuditEvent, ExecutionApproval, SandboxRun, Task
from company.workflow import Board
from test_migrations import migrate, migrated_engine  # noqa: F401

FAKE_IMAGE = "sha256:" + "a" * 64
FAST = sandbox.Limits(timeout_s=20, stored_bytes_per_stream=8 * 1024, max_output_bytes=256 * 1024)

PASSING = {"path": "test_ok.py", "content": (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_addition(self):\n"
    "        self.assertEqual(1 + 1, 2)\n")}
FAILING = {"path": "test_ko.py", "content": (
    "import unittest\n"
    "class T(unittest.TestCase):\n"
    "    def test_expected_failure_marker(self):\n"
    "        self.assertEqual(1, 2)\n")}


# ---------------------------------------------------------------- helpers

def engine_url(engine) -> str:
    if engine.dialect.name == "postgresql":
        with engine.connect() as c:
            schema = c.scalar(text("SELECT current_schema()"))
        return engine.url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    return engine.url.render_as_string(hide_password=False)


def generated_task(engine, tmp_path, monkeypatch, files=None):
    """MOCK meeting -> generation approval -> MOCK files; optionally developer fixture content."""
    migrate(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = replace(Settings.load(), mock=True)
    Board(sessions, settings, Budget(sessions, settings)).run_once()
    with sessions() as db:
        task_id = db.scalar(select(Task.id).where(Task.category == "prototype"))
    monkeypatch.setattr(prototyping, "ROOT", tmp_path)
    approve_task(sessions, task_id, actor="test-human")
    prototyping.generate_prototype(sessions, settings, Budget(sessions, settings), task_id)
    if files is not None:
        set_manifest(sessions, task_id, files)
    return sessions, task_id


def set_manifest(sessions, task_id, files):
    with sessions.begin() as db:
        db.get(Task, task_id).artifact_manifest = json.dumps({"provider": "fixture", "files": files})


def fake_executor(calls, state="PASSED"):
    def run(files, profile, image_id, *, run_id, limits, on_started=None):
        calls.append((files, profile, image_id, run_id))
        return sandbox.SandboxResult(state, 0, 5, "out", "err", 6, False, "",
                                     sandbox.NAME_PREFIX + run_id)
    return run


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    base = tmp_path / "sandbox-work"
    monkeypatch.setenv("SANDBOX_WORKDIR", str(base))
    return base


@pytest.fixture
def image_id():
    ref = os.getenv("TEST_SANDBOX_IMAGE")
    if not ref:
        pytest.skip("TEST_SANDBOX_IMAGE requis pour les tests Docker réels")
    docker = sandbox.docker_binary()
    if shutil.which(docker) is None:
        pytest.fail("TEST_SANDBOX_IMAGE défini mais binaire docker absent")
    found = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", ref], capture_output=True, text=True)
    if found.returncode != 0:
        pytest.fail(f"Image de test {ref} absente localement (aucun pull implicite)")
    return found.stdout.strip()


def leftover_containers():
    out = subprocess.run([sandbox.docker_binary(), "ps", "--all", "--filter", f"label={sandbox.LABEL}=1",
                          "--format", "{{.Names}}"], capture_output=True, text=True, check=True)
    return [n for n in out.stdout.split() if n.startswith(sandbox.NAME_PREFIX)]


def run_fixture(files, image_id, limits=FAST):
    return sandbox.execute(files, "python-unittest", image_id, limits=limits)


# ------------------------------------------------- approval logic (no Docker)

def test_generation_approval_alone_never_allows_execution(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    calls = []
    with pytest.raises(sandbox.SandboxRefused, match="Aucune autorisation"):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE},
                             executor=fake_executor(calls))
    assert calls == []
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(SandboxRun)) == 0


def test_unapproved_or_ungenerated_tasks_are_rejected(migrated_engine):
    migrate(migrated_engine)
    sessions = sessionmaker(migrated_engine, expire_on_commit=False)
    settings = replace(Settings.load(), mock=True)
    Board(sessions, settings, Budget(sessions, settings)).run_once()
    with sessions() as db:
        prototype = db.scalar(select(Task.id).where(Task.category == "prototype"))
        validation = db.scalar(select(Task.id).where(Task.category == "validation"))
    approve = dict(profile="python-unittest", image_id=FAKE_IMAGE, actor="alice", reason="revue faite")
    with pytest.raises(sandbox.SandboxRefused, match="jamais approuvé"):
        sandbox.approve_execution(sessions, prototype, **approve)
    approve_task(sessions, prototype, actor="alice")
    with pytest.raises(sandbox.SandboxRefused, match="non généré"):
        sandbox.approve_execution(sessions, prototype, **approve)
    with pytest.raises(sandbox.SandboxRefused, match="prototype inconnue"):
        sandbox.approve_execution(sessions, validation, **approve)
    for bad in (dict(approve, profile="shell"), dict(approve, image_id="python:latest"),
                dict(approve, actor=" "), dict(approve, reason="")):
        with pytest.raises(sandbox.SandboxRefused):
            sandbox.approve_execution(sessions, prototype, **bad)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ExecutionApproval)) == 0


def test_execution_approval_is_single_use_and_traced(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    images, calls = {"python-unittest": FAKE_IMAGE}, []
    approval = sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                                         actor="alice", reason="code relu")
    first = sandbox.run_approved(sessions, task_id, images=images, executor=fake_executor(calls))
    assert (first.state, first.attempt, first.approval_id) == ("PASSED", 1, approval)
    with pytest.raises(sandbox.SandboxRefused, match="Aucune autorisation"):
        sandbox.run_approved(sessions, task_id, images=images, executor=fake_executor(calls))
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                              actor="bob", reason="second essai")
    second = sandbox.run_approved(sessions, task_id, images=images, executor=fake_executor(calls, "FAILED"))
    assert (second.state, second.attempt) == ("FAILED", 2) and len(calls) == 2
    with sessions() as db:
        rows = db.scalars(select(SandboxRun).where(SandboxRun.task_id == task_id).order_by(SandboxRun.attempt)).all()
        assert [r.stdout for r in rows] == ["out", "out"] and all(r.finished_at for r in rows)
        approvals = db.scalars(select(ExecutionApproval)).all()
        assert {a.actor for a in approvals} == {"alice", "bob"} and all(a.consumed_at for a in approvals)
        kinds = {e.kind for e in db.scalars(select(AuditEvent))}
        assert {"execution_approved", "sandbox_started", "sandbox_passed", "sandbox_failed"} <= kinds


def test_approval_is_bound_to_content_and_image(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    calls = []
    approval = sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                                         actor="alice", reason="code relu")
    other_image = "sha256:" + "b" * 64
    with pytest.raises(sandbox.SandboxRefused, match="Image autorisée différente"):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": other_image},
                             executor=fake_executor(calls))
    with pytest.raises(sandbox.SandboxRefused, match="Image autorisée différente"):
        sandbox.run_approved(sessions, task_id, images={}, executor=fake_executor(calls))
    set_manifest(sessions, task_id, [PASSING])  # content changed after review
    with pytest.raises(sandbox.SandboxRefused, match="contenu a changé"):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE},
                             executor=fake_executor(calls))
    assert calls == []
    with sessions() as db:
        assert db.get(ExecutionApproval, approval).consumed_at is None  # refusals consume nothing


def test_executor_crash_is_recorded_and_not_replayed(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                              actor="alice", reason="code relu")

    def boom(*args, **kwargs):
        raise KeyError("SENSITIVE-DETAIL")
    run = sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE}, executor=boom)
    assert run.state == "ERROR" and "SENSITIVE" not in run.detail
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE}, executor=boom)


def test_docker_argv_isolation_without_model_content(tmp_path):
    hostile = "'; rm -rf / #$(curl evil)"
    argv = sandbox.docker_create_argv("docker", "aac-sbx-x", tmp_path / "ws", sandbox.PROFILES["python-unittest"],
                                      FAKE_IMAGE, sandbox.DEFAULT_LIMITS, 1)
    joined = " ".join(argv)
    for required in ("--network none", "--user 65534:65534", "--cap-drop ALL",
                     "--security-opt no-new-privileges:true", "--read-only", "--pull never",
                     "--pids-limit 64", "--memory 256m", "--memory-swap 256m", "--cpus 1",
                     "--ipc none", "--log-driver none", "readonly"):
        assert required in joined
    for forbidden in ("--privileged", "docker.sock", "--network host", "--pid host", "sh -c", "--cap-add",
                      "--env-file", "seccomp=unconfined", hostile):
        assert forbidden not in joined
    image_at = argv.index(FAKE_IMAGE)
    assert tuple(argv[image_at + 1:]) == sandbox.PROFILES["python-unittest"].argv[1:]
    assert '--entrypoint' in argv and 'HTTP_PROXY=' in argv
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.docker_create_argv("docker", "n", tmp_path, sandbox.PROFILES["python-unittest"],
                                   "python:3.12", sandbox.DEFAULT_LIMITS, 1)


def test_output_sink_bounds_and_sanitizes():
    sink = sandbox._Sink(10)
    sink.feed(b"abc\x00def")
    sink.feed(b"ghijklmnop")
    assert sink.seen == 17 and len(sink.data) == 10
    assert "\x00" not in sink.text() and sink.text().startswith("abc")


def test_image_allowlist_and_workdir_validation(monkeypatch):
    monkeypatch.delenv("SANDBOX_PYTHON_IMAGE_ID", raising=False)
    assert sandbox.allowed_images() == {}
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE_ID", "python:3.12-slim")
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.allowed_images()
    monkeypatch.setenv("SANDBOX_PYTHON_IMAGE_ID", FAKE_IMAGE)
    assert sandbox.allowed_images() == {name: FAKE_IMAGE for name in sandbox.PROFILES}
    monkeypatch.setenv("SANDBOX_WORKDIR", "/tmp/a,readonly=false")
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.workspace_base()
    with pytest.raises(ValueError):
        sandbox.Limits(timeout_s=0)


def test_cli_requires_explicit_review_and_configured_image(monkeypatch):
    from company.__main__ import sandbox_command
    args = Namespace(sandbox_action="approve", task_id="x", profile="python-unittest", actor="alice",
                     reason="r", reviewed=False)
    with pytest.raises(ValueError, match="--reviewed"):
        sandbox_command(args, None)
    monkeypatch.delenv("SANDBOX_PYTHON_IMAGE_ID", raising=False)
    with pytest.raises(ValueError, match="Aucune image"):
        sandbox_command(replace_ns(args, reviewed=True), None)


def replace_ns(ns, **changes):
    return Namespace(**{**vars(ns), **changes})


def test_populated_database_upgrades_to_sandbox_schema(migrated_engine):
    migrate(migrated_engine, "005_cost_review")
    with migrated_engine.begin() as c:
        c.execute(text("INSERT INTO runs(id,started_at,status,mode,mission) VALUES('r6',CURRENT_TIMESTAMP,'completed','mock','m')"))
        c.execute(text("INSERT INTO tasks(id,run_id,title,description,category,status,created_at,value_eur,notes) "
                       "VALUES('t6','r6','t','d','prototype','approved',CURRENT_TIMESTAMP,0,'')"))
    migrate(migrated_engine)
    migrate(migrated_engine)
    from sqlalchemy import inspect
    tables = set(inspect(migrated_engine).get_table_names())
    assert {"execution_approvals", "sandbox_runs", "tasks"} <= tables
    with migrated_engine.connect() as c:
        assert c.scalar(text("SELECT status FROM tasks WHERE id='t6'")) == "approved"


# ------------------------------------------------------- real Docker isolation

def test_success_and_functional_failure(image_id, workdir):
    ok = run_fixture([PASSING], image_id)
    assert (ok.state, ok.exit_code) == ("PASSED", 0) and "test_addition" in ok.stderr
    ko = run_fixture([FAILING], image_id)
    assert ko.state == "FAILED" and ko.exit_code == 1 and "test_expected_failure_marker" in ko.stderr
    assert leftover_containers() == [] and list(workdir.iterdir()) == []


def test_timeout_kills_process_that_ignores_sigterm(image_id, workdir):
    stubborn = {"path": "test_stubborn.py", "content": (
        "import signal, time, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_never_ends(self):\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        while True:\n"
        "            time.sleep(0.05)\n")}
    started = time.monotonic()
    result = run_fixture([stubborn], image_id, replace(FAST, timeout_s=3))
    assert result.state == "TIMEOUT"
    assert time.monotonic() - started < 3 + 30
    assert leftover_containers() == [] and list(workdir.iterdir()) == []


def test_network_is_unreachable(image_id, workdir):
    probe = {"path": "test_network.py", "content": (
        "import socket, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_tcp_blocked(self):\n"
        "        with self.assertRaises(OSError):\n"
        "            socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    def test_dns_blocked(self):\n"
        "        with self.assertRaises(OSError):\n"
        "            socket.getaddrinfo('example.com', 443)\n"
        "    def test_only_loopback(self):\n"
        "        names = {name for _, name in socket.if_nameindex()}\n"
        "        self.assertEqual(names, {'lo'})\n")}
    result = run_fixture([probe], image_id)
    assert result.state == "PASSED", result.stderr


def test_host_files_secrets_and_privileges_are_out_of_reach(image_id, workdir, tmp_path, monkeypatch):
    canary = uuid4().hex
    host_secret = tmp_path / "host-secret.txt"
    host_secret.write_text(canary)
    monkeypatch.setenv("OPENAI_API_KEY", f"sk-{canary}")
    repo = Path(__file__).resolve().parents[1]
    probe = {"path": "test_escape.py", "content": (
        "import os, unittest\n"
        f"HOST_SECRET = {str(host_secret)!r}\nREPO = {str(repo)!r}\nCANARY = {canary!r}\n"
        "class T(unittest.TestCase):\n"
        "    def test_host_paths_absent(self):\n"
        "        self.assertFalse(os.path.exists(HOST_SECRET))\n"
        "        self.assertFalse(os.path.exists(os.path.join(REPO, '.env')))\n"
        "        self.assertFalse(os.path.exists(os.path.join(REPO, 'company')))\n"
        "        for sock in ('/var/run/docker.sock', '/run/docker.sock'):\n"
        "            self.assertFalse(os.path.exists(sock))\n"
        "    def test_no_secret_in_environment(self):\n"
        "        self.assertFalse(any(CANARY in v for v in os.environ.values()))\n"
        "        self.assertNotIn('OPENAI_API_KEY', os.environ)\n"
        "    def test_unprivileged(self):\n"
        "        self.assertNotEqual(os.getuid(), 0)\n"
        "        status = dict(l.split(':', 1) for l in open('/proc/self/status').read().splitlines() if ':' in l)\n"
        "        self.assertEqual(int(status['CapEff'].strip(), 16), 0)\n"
        "        self.assertEqual(status['NoNewPrivs'].strip(), '1')\n"
        "    def test_filesystem_read_only_except_tmp(self):\n"
        "        for target in ('/workspace/new.txt', '/escape.txt', '/workspace/test_escape.py'):\n"
        "            with self.assertRaises(OSError):\n"
        "                open(target, 'a').write('x')\n"
        "        with open('/tmp/scratch.txt', 'w') as f:\n"
        "            f.write('ok')\n")}
    result = run_fixture([probe], image_id)
    assert result.state == "PASSED", result.stderr
    assert host_secret.read_text() == canary


def test_excessive_output_is_stopped_and_bounded(image_id, workdir):
    noisy = {"path": "test_noisy.py", "content": (
        "import sys, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_flood(self):\n"
        "        while True:\n"
        "            sys.stdout.write('x' * 4096)\n")}
    result = run_fixture([noisy], image_id)
    assert result.state == "OUTPUT_LIMIT" and result.truncated
    assert len(result.stdout.encode()) <= FAST.stored_bytes_per_stream
    assert result.output_bytes > FAST.max_output_bytes
    assert leftover_containers() == []


def test_memory_limit_is_enforced(image_id, workdir):
    hungry = {"path": "test_memory.py", "content": (
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_allocate(self):\n"
        "        blocks = [bytearray(16 * 1024 * 1024) for _ in range(40)]\n"
        "        self.assertTrue(blocks)\n")}
    result = run_fixture([hungry], image_id, replace(FAST, memory="64m"))
    assert result.state in ("OOM", "FAILED") and result.state != "PASSED"
    assert result.exit_code != 0


def _interrupted_child(url, task_id, workdir):
    os.environ["SANDBOX_WORKDIR"] = workdir
    engine = create_engine(url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    images = {"python-unittest": os.environ["CHILD_IMAGE_ID"]}
    sandbox.run_approved(sessions, task_id, images=images, limits=replace(FAST, timeout_s=60),
                         on_started=lambda name: os._exit(17))


def test_interrupted_run_is_cleaned_up_and_never_replayed(image_id, workdir, migrated_engine, tmp_path, monkeypatch):
    sleeper = {"path": "test_sleep.py", "content": (
        "import time, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_long(self):\n"
        "        time.sleep(60)\n")}
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch, files=[sleeper])
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=image_id,
                              actor="alice", reason="fixture développeur")
    monkeypatch.setenv("CHILD_IMAGE_ID", image_id)
    child = multiprocessing.get_context("spawn").Process(
        target=_interrupted_child, args=(engine_url(migrated_engine), task_id, str(workdir)))
    child.start()
    child.join(60)
    if child.is_alive():
        child.kill(); child.join(); pytest.fail("child timed out")
    assert child.exitcode == 17
    assert len(leftover_containers()) == 1  # the orphan keeps running until cleanup
    with sessions() as db:
        run = db.scalar(select(SandboxRun).where(SandboxRun.task_id == task_id))
        assert run.state == "RUNNING"
    with pytest.raises(sandbox.SandboxRefused, match="Aucune autorisation"):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": image_id})
    assert sandbox.cleanup(sessions)["containers_removed"] == []  # deadline not reached yet
    report = sandbox.cleanup(sessions, now=time.time() + 10_000)
    assert report["runs_interrupted"] == [run.id] and len(report["containers_removed"]) == 1
    assert leftover_containers() == [] and list(workdir.iterdir()) == []
    with sessions() as db:
        assert db.get(SandboxRun, run.id).state == "INTERRUPTED"


def test_end_to_end_results_attached_to_task_and_attempt(image_id, workdir, migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch, files=[PASSING, FAILING])
    images = {"python-unittest": image_id}
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=image_id,
                              actor="alice", reason="fixtures développeur relues")
    run = sandbox.run_approved(sessions, task_id, images=images, limits=FAST)
    assert run.state == "FAILED" and run.attempt == 1 and run.image_id == image_id
    with sessions() as db:
        stored = db.get(SandboxRun, run.id)
        assert stored.task_id == task_id and "test_expected_failure_marker" in stored.stderr
        assert stored.duration_ms > 0 and stored.finished_at is not None
    set_manifest(sessions, task_id, [PASSING])
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.run_approved(sessions, task_id, images=images, limits=FAST)
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=image_id,
                              actor="alice", reason="correction relue")
    second = sandbox.run_approved(sessions, task_id, images=images, limits=FAST)
    assert (second.state, second.attempt) == ("PASSED", 2)
    assert leftover_containers() == []
