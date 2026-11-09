"""Revue Lot 3 (suite) : régressions et validation réelle du sandbox.

Code exécuté dans Docker : uniquement des fixtures écrites par les développeurs.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from company import prototyping, sandbox
from company.database import AuditEvent, ExecutionApproval, SandboxCleanup, SandboxRun
from company.static_validation import validate
from test_migrations import migrate, migrated_engine  # noqa: F401
from test_sandbox import (FAKE_IMAGE, FAST, PASSING, engine_url, fake_executor, generated_task,  # noqa: F401
                          image_id, leftover_containers, set_manifest, workdir)

HTML = "<html><head><title>t</title>{head}</head><body>{body}</body></html>"


def caps_json(value: bool) -> bytes:
    return json.dumps({"MemoryLimit": value, "SwapLimit": value, "PidsLimit": value, "CpuCfsQuota": value,
                       "CgroupVersion": "2", "SecurityOptions": ["name=rootless"]}).encode()


# ------------------------------------------------ A. profil static-html

@pytest.mark.parametrize("head,body", [
    ("", '<img srcset="https://evil.test/x.png 1x">'),
    ('<meta http-equiv="refresh" content="0;url=https://evil.test">', ""),
    ("", '<form action="https://evil.test/steal"></form>'),
    ("", '<button formaction="//evil.test">x</button>'),
    ("", '<div style="background:url(https://evil.test/t.png)"></div>'),
    ('<style>@import "https://evil.test/a.css";</style>', ""),
    ('<base href="/">', ""),
    ("", '<object data="https://evil.test/x"></object>'),
    ("", '<img src="%2e%2e/%2e%2e/etc/passwd">'),
    ("", '<a href="java\tscript:alert(1)">x</a>'),
    ("", '<img src="data:image/png;base64,AAAA">'),
    ("", '<img src="missing.png">'),
])
def test_static_html_refuses_external_or_escaping_references(tmp_path, head, body):
    (tmp_path / "index.html").write_text(HTML.format(head=head, body=body))
    with pytest.raises(ValueError):
        validate(tmp_path)


def test_static_html_checks_every_page_stylesheet_and_symlink(tmp_path):
    (tmp_path / "ok.png").write_bytes(b"x")
    (tmp_path / "app.js").write_text("fetch('https://never.executed')")
    (tmp_path / "s.css").write_text('body{background:url("ok.png")}')
    (tmp_path / "index.html").write_text(HTML.format(
        head='<link rel="stylesheet" href="s.css">',
        body='<img srcset="ok.png 1x, ok.png 2x"><a href="#top">t</a><a href="page2.html">p</a>'
             '<script src="app.js"></script>'))
    (tmp_path / "page2.html").write_text(HTML.format(head="", body="<p>local</p>"))
    validate(tmp_path)  # legitimate local prototype
    (tmp_path / "page2.html").write_text(HTML.format(head="", body='<script src="https://evil.test/x.js"></script>'))
    with pytest.raises(ValueError, match="page2.html"):
        validate(tmp_path)
    (tmp_path / "page2.html").write_text(HTML.format(head="", body=""))
    (tmp_path / "s.css").write_text('@import url("https://evil.test/a.css");')
    with pytest.raises(ValueError, match="s.css"):
        validate(tmp_path)
    (tmp_path / "s.css").write_text("")
    (tmp_path / "link.png").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="Symbolic"):
        validate(tmp_path)


# ------------------------------------------------ B. base de travail

def test_workspace_base_refuses_symlink_and_shared_permissions(tmp_path, monkeypatch):
    target = tmp_path / "owned-by-someone"
    target.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(target)
    monkeypatch.setenv("SANDBOX_WORKDIR", str(tmp_path / "link"))
    with pytest.raises(sandbox.SandboxRefused, match="lien"):
        sandbox.secure_base()
    monkeypatch.setenv("SANDBOX_WORKDIR", str(target))
    target.chmod(0o777)
    with pytest.raises(sandbox.SandboxRefused, match="0700"):
        sandbox.secure_base()
    monkeypatch.setenv("SANDBOX_WORKDIR", str(tmp_path / "fresh"))
    base = sandbox.secure_base()
    assert base.is_dir() and base.stat().st_mode & 0o777 == 0o700


def test_execute_refuses_unsafe_base_before_touching_docker(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sandbox, "_docker", lambda *a, **k: calls.append(a))
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv("SANDBOX_WORKDIR", str(shared))
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.execute([PASSING], "python-unittest", FAKE_IMAGE)
    assert calls == [] and list(shared.iterdir()) == []


# ------------------------------------------------ C. sorties non fiables

def test_display_safe_neutralizes_terminal_escapes():
    hostile = "ok\x1b[2J\x1b]52;c;ZXZpbA==\x07 fin\u202eevil\r\nligne é\tà"
    shown = sandbox.display_safe(hostile)
    assert "\x1b" not in shown and "\x07" not in shown and "\u202e" not in shown and "\r" not in shown
    assert "\\x1b" in shown and "\nligne é\tà" in shown


# ------------------------------------------------ D. approbation et disque relu

def test_approval_refused_when_reviewed_files_differ_or_are_missing(migrated_engine, tmp_path, monkeypatch):
    import shutil
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    root = prototyping.ROOT / "artifacts" / "prototypes" / task_id
    approve = dict(profile="python-unittest", image_id=FAKE_IMAGE, actor="alice", reason="relu")
    (root / "index.html").write_text("<h1>version relue, différente</h1>")
    with pytest.raises(sandbox.SandboxRefused, match="différents"):
        sandbox.approve_execution(sessions, task_id, **approve)
    shutil.rmtree(root)
    with pytest.raises(sandbox.SandboxRefused, match="introuvables"):
        sandbox.approve_execution(sessions, task_id, **approve)
    set_manifest(sessions, task_id, [PASSING])  # DB and disk agree again
    assert sandbox.approve_execution(sessions, task_id, **approve)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(ExecutionApproval)) == 1


# ------------------------------------------------ E. contrôle préalable

def test_incapable_daemon_refused_without_consuming_approval(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    monkeypatch.setenv("SANDBOX_WORKDIR", str(tmp_path / "sbx"))
    approval = sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                                         actor="alice", reason="relu")
    monkeypatch.setattr(sandbox, "_docker", lambda d, *a, **k: subprocess.CompletedProcess(a, 0, caps_json(False), b""))
    with pytest.raises(sandbox.SandboxRefused, match="aucune autorisation consommée"):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE})
    with sessions() as db:
        assert db.get(ExecutionApproval, approval).consumed_at is None
        assert db.scalar(select(func.count()).select_from(SandboxRun)) == 0


def test_capabilities_parse_docker_json_keys(monkeypatch):
    monkeypatch.setattr(sandbox, "_docker", lambda d, *a, **k: subprocess.CompletedProcess(a, 0, caps_json(True), b""))
    caps = sandbox.docker_capabilities("docker")
    assert caps["reachable"] and caps["missing"] == [] and caps["rootless"] and caps["cgroup_version"] == "2"
    monkeypatch.setattr(sandbox, "_docker", lambda d, *a, **k: subprocess.CompletedProcess(a, 1, b"", b"down"))
    assert sandbox.docker_capabilities("docker")["reachable"] is False


@pytest.mark.parametrize("observed,problems", [
    ({"memory": "268435456", "memsw_v1": "268435456", "pids": "64", "quota_v1": "100000",
      "period_v1": "100000", "uid": 65534}, []),
    ({"memory": "268435456", "swap_v2": "0", "pids": "64", "cpu_v2": "100000 100000", "uid": 65534}, []),
    ({"memory": "max", "swap_v2": "max", "pids": "max", "cpu_v2": "max 100000", "uid": 65534},
     ["mémoire", "swap", "processus", "CPU"]),
    ({"memory": "268435456", "swap_v2": "0", "pids": "64", "cpu_v2": "100000 100000", "uid": 0},
     ["utilisateur non root"]),
    ({}, ["mémoire", "swap", "processus", "CPU", "utilisateur non root"]),
])
def test_limits_mismatch_detects_unapplied_limits(observed, problems):
    assert sandbox.limits_mismatch(observed, sandbox.DEFAULT_LIMITS) == problems


# ------------------------------------------------ F. résultat tardif

def test_late_result_after_interruption_is_kept_as_evidence(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                              actor="alice", reason="relu")

    def executor(files, profile, image, *, run_id, limits, on_started=None):
        with sessions.begin() as db:  # a forced cleanup happens while the test runs
            db.get(SandboxRun, run_id).state = "INTERRUPTED"
        return sandbox.SandboxResult("PASSED", 0, 42, "late stdout", "late stderr", 22, False)
    run = sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE}, executor=executor)
    assert run.state == "INTERRUPTED" and run.stdout == "late stdout" and run.exit_code == 0
    assert "résultat tardif : PASSED" in run.detail
    with sessions() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.kind == "sandbox_late_result"))


# ------------------------------------------------ nettoyage ciblé et diagnostic (sans Docker)

def _orphan(sessions, task_id, monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_WORKDIR", str(tmp_path / "sbx"))
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=FAKE_IMAGE,
                              actor="alice", reason="relu")

    def crash(files, profile, image, *, run_id, limits, on_started=None):
        raise SystemExit  # parent dies mid-run: record stays RUNNING
    with pytest.raises(SystemExit):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": FAKE_IMAGE}, executor=crash)
    with sessions() as db:
        return db.scalar(select(SandboxRun.id))


def test_targeted_cleanup_is_audited_and_never_reexecutes(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    run_id = _orphan(sessions, task_id, monkeypatch, tmp_path)
    workspace = sandbox.secure_base() / run_id
    workspace.mkdir(mode=0o700)
    name = sandbox.NAME_PREFIX + run_id
    state = {"containers": {name: {"deadline": int(time.time()) + 600, "state": "running"}}}
    monkeypatch.setattr(sandbox, "_containers", lambda docker: dict(state["containers"]))
    removed = []

    def remove(docker, container):
        removed.append(container)
        state["containers"].pop(container, None)
    monkeypatch.setattr(sandbox, "_remove", remove)
    for bad in (dict(actor="", reason="r"), dict(actor="a", reason=""), dict(actor="a\nb", reason="r")):
        with pytest.raises(sandbox.SandboxRefused):
            sandbox.cleanup_run(sessions, run_id, **bad)
    with pytest.raises(sandbox.SandboxRefused):
        sandbox.cleanup_run(sessions, "../" + run_id, actor="a", reason="r")
    assert sandbox.diagnose(sessions)["running"][0]["status"] == "actif"
    with pytest.raises(sandbox.SandboxRefused, match="actif"):
        sandbox.cleanup_run(sessions, run_id, actor="julien", reason="parent tué")
    assert removed == [] and workspace.exists()
    report = sandbox.cleanup_run(sessions, run_id, actor="julien", reason="parent tué",
                                 now=time.time() + 10_000)
    assert report["outcome"] == "interrupted" and report["containers_removed"] == [name]
    assert report["workspace_removed"] and not workspace.exists()
    again = sandbox.cleanup_run(sessions, run_id, actor="julien", reason="vérification")
    assert again["outcome"] == "nothing_to_clean"
    with sessions() as db:
        assert db.get(SandboxRun, run_id).state == "INTERRUPTED"
        outcomes = [c.outcome for c in db.scalars(select(SandboxCleanup).order_by(SandboxCleanup.created_at))]
        assert outcomes == ["refused_active", "interrupted", "nothing_to_clean"]
        assert db.scalar(select(func.count()).select_from(SandboxRun)) == 1
        assert db.scalar(select(func.count()).select_from(ExecutionApproval)
                         .where(ExecutionApproval.consumed_at.is_(None))) == 0


def test_targeted_cleanup_never_touches_another_run(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    run_id = _orphan(sessions, task_id, monkeypatch, tmp_path)
    other = "b" * 32
    (sandbox.secure_base() / other).mkdir(mode=0o700)
    containers = {sandbox.NAME_PREFIX + other: {"deadline": 1, "state": "exited"},
                  sandbox.NAME_PREFIX + run_id: {"deadline": 1, "state": "exited"}}
    monkeypatch.setattr(sandbox, "_containers", lambda docker: dict(containers))
    removed = []
    monkeypatch.setattr(sandbox, "_remove", lambda docker, name: (removed.append(name), containers.pop(name)))
    sandbox.cleanup_run(sessions, run_id, actor="julien", reason="orphelin")
    assert removed == [sandbox.NAME_PREFIX + run_id]
    assert (sandbox.secure_base() / other).exists()
    diagnosis = sandbox.diagnose(sessions)
    assert diagnosis["unknown_containers"] == [sandbox.NAME_PREFIX + other]


def test_diagnose_classifies_orphans(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    _orphan(sessions, task_id, monkeypatch, tmp_path)
    monkeypatch.setattr(sandbox, "docker_capabilities",
                        lambda docker=None: {"reachable": True, "missing": [], "error": None})
    monkeypatch.setattr(sandbox, "_containers", lambda docker: {})
    assert sandbox.diagnose(sessions)["running"][0]["status"] == "orphelin : conteneur absent"
    assert sandbox.diagnose(sessions, now=time.time() + 10_000)["running"][0]["status"] == \
        "orphelin : échéance dépassée"


def test_migration_008_on_populated_database(migrated_engine):
    migrate(migrated_engine, "007_work_reviews")
    with migrated_engine.begin() as c:
        c.execute(text("INSERT INTO runs(id,started_at,status,mode,mission) VALUES('r8',CURRENT_TIMESTAMP,'completed','mock','m')"))
        c.execute(text("INSERT INTO tasks(id,run_id,title,description,category,status,created_at,value_eur,notes) "
                       "VALUES('t8','r8','t','d','prototype','awaiting_evaluation',CURRENT_TIMESTAMP,0,'')"))
        c.execute(text("INSERT INTO execution_approvals(id,task_id,manifest_sha256,profile,image_id,actor,reason,created_at,consumed_at) "
                       "VALUES('a8','t8','x','python-unittest','sha256:x','alice','r',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO sandbox_runs(id,task_id,approval_id,attempt,profile,image_id,manifest_sha256,container_name,"
                       "timeout_s,state,started_at,stdout,stderr,output_bytes,truncated,detail) VALUES('s8','t8','a8',1,"
                       "'python-unittest','sha256:x','x','aac-sbx-s8',60,'RUNNING',CURRENT_TIMESTAMP,'','',0,false,'')"))
    migrate(migrated_engine)
    migrate(migrated_engine)
    with migrated_engine.connect() as c:
        assert c.scalar(text("SELECT state FROM sandbox_runs WHERE id='s8'")) == "RUNNING"
        assert c.scalar(text("SELECT count(*) FROM sandbox_cleanups")) == 0


# ------------------------------------------------ validation Docker réelle

PROBE = {"path": "test_limits.py", "content": (
    "import os, time, resource, unittest, multiprocessing\n"
    "def busy(seconds):\n"
    "    end = time.monotonic() + seconds\n"
    "    while time.monotonic() < end:\n"
    "        pass\n"
    "class T(unittest.TestCase):\n"
    "    def test_pids_limit(self):\n"
    "        children, blocked = [], False\n"
    "        try:\n"
    "            for _ in range(300):\n"
    "                pid = os.fork()\n"
    "                if pid == 0:\n"
    "                    time.sleep(2); os._exit(0)\n"
    "                children.append(pid)\n"
    "        except OSError:\n"
    "            blocked = True\n"
    "        for pid in children:\n"
    "            os.waitpid(pid, 0)\n"
    "        print('forks', len(children))\n"
    "        self.assertTrue(blocked)\n"
    "        self.assertLess(len(children), 64)\n"
    "    def test_cpu_quota(self):\n"
    "        start = time.monotonic()\n"
    "        workers = [multiprocessing.Process(target=busy, args=(1.5,)) for _ in range(4)]\n"
    "        [w.start() for w in workers]; [w.join() for w in workers]\n"
    "        usage = resource.getrusage(resource.RUSAGE_CHILDREN)\n"
    "        ratio = (usage.ru_utime + usage.ru_stime) / (time.monotonic() - start)\n"
    "        print('cpu ratio %.2f' % ratio)\n"
    "        self.assertLess(ratio, 0.5 + 0.15)\n")}


def test_pids_and_cpu_limits_are_really_applied(image_id, workdir):
    result = sandbox.execute([PROBE], "python-unittest", image_id, limits=replace(FAST, cpus="0.5", timeout_s=60))
    assert result.state == "PASSED", result.stdout + result.stderr
    assert "forks" in result.stdout and "cpu ratio" in result.stdout


def test_option_silently_not_applied_is_refused(image_id, workdir, monkeypatch):
    original = sandbox.docker_create_argv

    def without_pids_limit(*args, **kwargs):
        argv = original(*args, **kwargs)
        at = argv.index("--pids-limit")
        return argv[:at] + argv[at + 2:]
    monkeypatch.setattr(sandbox, "docker_create_argv", without_pids_limit)
    result = sandbox.execute([PASSING], "python-unittest", image_id, limits=FAST)
    assert result.state == "ERROR" and "processus" in result.detail
    assert result.stdout == "" and leftover_containers() == [] and list(workdir.iterdir()) == []


def test_concurrent_runs_are_isolated(image_id, workdir):
    writer = {"path": "test_writer.py", "content": (
        "import os, time, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_write_then_wait(self):\n"
        "        open('/tmp/run-a-secret', 'w').write('A')\n"
        "        time.sleep(4)\n"
        "        self.assertEqual(sorted(os.listdir('/workspace')), ['test_writer.py'])\n")}
    reader = {"path": "test_reader.py", "content": (
        "import os, time, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_cannot_see_other_run(self):\n"
        "        time.sleep(1.5)\n"
        "        self.assertFalse(os.path.exists('/tmp/run-a-secret'))\n"
        "        self.assertEqual(sorted(os.listdir('/workspace')), ['test_reader.py'])\n")}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(sandbox.execute, [writer], "python-unittest", image_id, limits=FAST)
        second = pool.submit(sandbox.execute, [reader], "python-unittest", image_id, limits=FAST)
        a, b = first.result(), second.result()
    assert (a.state, b.state) == ("PASSED", "PASSED"), a.stderr + b.stderr
    assert a.container_name != b.container_name
    assert leftover_containers() == [] and list(workdir.iterdir()) == []


def test_docker_error_leaves_nothing_behind(image_id, workdir):
    missing_image = "sha256:" + "0" * 64
    result = sandbox.execute([PASSING], "python-unittest", missing_image, limits=FAST)
    assert result.state == "ERROR"
    assert leftover_containers() == [] and list(workdir.iterdir()) == []


def test_static_html_profile_in_container(image_id, workdir):
    good = [{"path": "index.html", "content": HTML.format(head="", body='<script src="app.js"></script>')},
            {"path": "app.js", "content": "throw new Error('never executed')"}]
    bad = [{"path": "index.html", "content": HTML.format(head="", body='<img srcset="https://evil.test/a 1x">')}]
    ok = sandbox.execute(good, "static-html", image_id, limits=FAST)
    ko = sandbox.execute(bad, "static-html", image_id, limits=FAST)
    assert ok.state == "PASSED" and "JavaScript not executed" in ok.stdout
    assert ko.state == "FAILED" and "External" in ko.stderr


def _killed_parent(url, task_id, workdir, marker):
    os.environ["SANDBOX_WORKDIR"] = workdir
    sessions = sessionmaker(create_engine(url), expire_on_commit=False)
    sleeper_limits = replace(FAST, timeout_s=60)
    sandbox.run_approved(sessions, task_id, images={"python-unittest": os.environ["CHILD_IMAGE_ID"]},
                         limits=sleeper_limits, on_started=lambda name: Path(marker).write_text(name))


def test_sigkill_of_parent_then_targeted_recovery(image_id, workdir, migrated_engine, tmp_path, monkeypatch):
    sleeper = {"path": "test_sleep.py", "content": (
        "import time, unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_long(self):\n"
        "        time.sleep(60)\n")}
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch, files=[sleeper])
    sandbox.approve_execution(sessions, task_id, profile="python-unittest", image_id=image_id,
                              actor="alice", reason="fixture développeur")
    monkeypatch.setenv("CHILD_IMAGE_ID", image_id)
    marker = tmp_path / "started"
    child = multiprocessing.get_context("spawn").Process(
        target=_killed_parent, args=(engine_url(migrated_engine), task_id, str(workdir), str(marker)))
    child.start()
    try:
        _recover_after_sigkill(child, marker, sessions, task_id, image_id, workdir)
    finally:
        if child.is_alive():
            child.kill()
        for name in leftover_containers():  # never leak into other tests, even on failure
            subprocess.run([sandbox.docker_binary(), "rm", "-f", name], capture_output=True)


def _recover_after_sigkill(child, marker, sessions, task_id, image_id, workdir):
    for _ in range(300):
        if marker.exists() or not child.is_alive():
            break
        time.sleep(0.1)
    assert marker.exists(), "le conteneur n'a pas démarré"
    child.kill()  # SIGKILL: no finally block, no Python cleanup
    child.join(10)
    with sessions() as db:
        run = db.scalar(select(SandboxRun).where(SandboxRun.task_id == task_id))
    assert run.state == "RUNNING" and leftover_containers() == [run.container_name]
    assert sandbox.diagnose(sessions)["running"][0]["status"] == "actif"  # created or running
    with pytest.raises(sandbox.SandboxRefused, match="actif"):
        sandbox.cleanup_run(sessions, run.id, actor="julien", reason="parent tué")
    assert leftover_containers() == [run.container_name]  # refusal removed nothing
    report = sandbox.cleanup_run(sessions, run.id, actor="julien", reason="parent tué",
                                 now=time.time() + 10_000)
    assert report["outcome"] == "interrupted" and report["workspace_removed"]
    assert leftover_containers() == [] and list(workdir.iterdir()) == []
    with pytest.raises(sandbox.SandboxRefused, match="Aucune autorisation"):
        sandbox.run_approved(sessions, task_id, images={"python-unittest": image_id})


def test_starting_container_counts_as_active(migrated_engine, tmp_path, monkeypatch):
    sessions, task_id = generated_task(migrated_engine, tmp_path, monkeypatch)
    run_id = _orphan(sessions, task_id, monkeypatch, tmp_path)
    containers = {sandbox.NAME_PREFIX + run_id: {"deadline": int(time.time()) + 600, "state": "created"}}
    monkeypatch.setattr(sandbox, "_containers", lambda docker: dict(containers))
    monkeypatch.setattr(sandbox, "_remove", lambda *a: pytest.fail("starting container removed"))
    monkeypatch.setattr(sandbox, "docker_capabilities",
                        lambda docker=None: {"reachable": True, "missing": [], "error": None})
    assert sandbox.diagnose(sessions)["running"][0]["status"] == "actif"
    with pytest.raises(sandbox.SandboxRefused, match="actif"):
        sandbox.cleanup_run(sessions, run_id, actor="julien", reason="trop tôt")
