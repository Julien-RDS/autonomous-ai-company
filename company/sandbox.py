"""Lot 3 : tests de prototypes dans un conteneur Docker isolé, jamais sur l'hôte.

Principes :
- l'approbation de GÉNÉRATION ne permet pas l'exécution ; une approbation
  d'EXÉCUTION distincte, à usage unique, est liée au contenu exact (SHA-256 du
  manifeste), au profil de test et à l'identifiant immuable de l'image ;
- la commande de test est définie par l'application (profil), sous forme de
  liste d'arguments : aucun shell, aucun contenu issu d'un modèle dans argv ;
- réseau coupé, utilisateur non root, capacités supprimées, no-new-privileges,
  racine en lecture seule, /tmp en tmpfs borné, limites mémoire/CPU/processus,
  sorties bornées, conteneur détruit après succès, échec, dépassement ou erreur ;
- aucune image n'est téléchargée implicitement (--pull never) ; seule une image
  dont l'ID sha256 est explicitement autorisé peut être lancée.

Docker réduit les risques mais n'est pas une frontière de sécurité absolue :
voir docs/LOT_3_SANDBOX.md.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from .database import Approval, AuditEvent, ExecutionApproval, SandboxRun, Task, new_id, utcnow
from .prototyping import safe_files

IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
LABEL = "aac.sandbox"
NAME_PREFIX = "aac-sbx-"
DOCKER_CALL_TIMEOUT = 60
CLEANUP_GRACE_S = 120


@dataclass(frozen=True)
class Profile:
    """Test command owned by the application, never by a model."""
    name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]


PROFILES = {
    "python-unittest": Profile(
        name="python-unittest",
        # -I: isolated mode (ignores PYTHON* variables and user site); -B: no .pyc
        # on the read-only workspace. unittest discovery imports test_*.py files.
        argv=("python", "-I", "-B", "-m", "unittest", "discover",
              "-s", "/workspace", "-t", "/workspace", "-p", "test_*.py", "-v"),
        env=(("HOME", "/tmp"), ("PYTHONDONTWRITEBYTECODE", "1"), ("LANG", "C.UTF-8")),
    ),
}


@dataclass(frozen=True)
class Limits:
    timeout_s: int = 60
    memory: str = "256m"
    cpus: str = "1"
    pids: int = 64
    tmpfs_size: str = "32m"
    nofile: int = 256
    stored_bytes_per_stream: int = 64 * 1024
    max_output_bytes: int = 1024 * 1024

    def __post_init__(self):
        if not 1 <= self.timeout_s <= 900 or not 8 <= self.pids <= 1024:
            raise ValueError("Limites de sandbox hors bornes")
        if not 1024 <= self.stored_bytes_per_stream <= self.max_output_bytes <= 16 * 1024 * 1024:
            raise ValueError("Limites de sortie hors bornes")


DEFAULT_LIMITS = Limits()


@dataclass
class SandboxResult:
    state: str
    exit_code: int | None
    duration_ms: int
    stdout: str
    stderr: str
    output_bytes: int
    truncated: bool
    detail: str = ""
    container_name: str = ""


class SandboxError(RuntimeError):
    pass


class SandboxRefused(ValueError):
    """Execution not authorized or not configured; nothing was started."""


def docker_binary() -> str:
    return os.getenv("SANDBOX_DOCKER_BIN", "docker")


def workspace_base() -> Path:
    base = Path(os.getenv("SANDBOX_WORKDIR") or Path(tempfile.gettempdir()) / "aac-sandbox").resolve()
    # The path is embedded in a --mount specification: refuse its separators.
    if any(c in str(base) for c in ",=\n\"'"):
        raise SandboxRefused("SANDBOX_WORKDIR contient un caractère interdit")
    return base


def allowed_images() -> dict[str, str]:
    """Explicit allowlist: profile -> immutable local image ID (sha256:...)."""
    image = os.getenv("SANDBOX_PYTHON_IMAGE_ID", "").strip()
    if not image:
        return {}
    if not IMAGE_ID.match(image):
        raise SandboxRefused("SANDBOX_PYTHON_IMAGE_ID doit être un identifiant sha256:<64 hex>")
    return {"python-unittest": image}


def manifest_digest(files: list[dict]) -> str:
    canonical = json.dumps([{"path": f["path"], "content": f["content"]} for f in files],
                           ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def docker_create_argv(docker: str, name: str, workspace: Path, profile: Profile, image_id: str,
                       limits: Limits, deadline_epoch: int) -> list[str]:
    """Pure function, unit-tested: every isolation option in one place."""
    if not IMAGE_ID.match(image_id):
        raise SandboxRefused("Image non identifiée par un ID sha256")
    argv = [
        docker, "create",
        "--name", name,
        "--label", f"{LABEL}=1",
        "--label", f"aac.deadline={deadline_epoch}",
        "--pull", "never",
        "--network", "none",
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--read-only",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,noexec,size={limits.tmpfs_size},mode=1777",
        "--memory", limits.memory,
        "--memory-swap", limits.memory,
        "--cpus", limits.cpus,
        "--pids-limit", str(limits.pids),
        "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
        "--ulimit", "core=0",
        "--ipc", "none",
        "--log-driver", "none",
        "--init",
        "--hostname", "sandbox",
        "--workdir", "/workspace",
        "--mount", f"type=bind,source={workspace},target=/workspace,readonly",
    ]
    for key, value in profile.env:
        argv += ["--env", f"{key}={value}"]
    return argv + [image_id, *profile.argv]


class _Sink:
    """Keeps at most `stored` bytes; counts everything observed."""

    def __init__(self, stored: int):
        self.stored, self.data, self.seen = stored, bytearray(), 0

    def feed(self, chunk: bytes):
        self.seen += len(chunk)
        room = self.stored - len(self.data)
        if room > 0:
            self.data += chunk[:room]

    def text(self) -> str:
        # PostgreSQL TEXT rejects NUL bytes; output is untrusted data.
        return bytes(self.data).decode("utf-8", errors="replace").replace("\x00", "\ufffd")


def _docker(docker: str, *args: str, check=False, timeout=DOCKER_CALL_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([docker, *args], capture_output=True, timeout=timeout, check=check,
                          stdin=subprocess.DEVNULL)


def _discard_workspace(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for item in [path, *path.rglob("*")]:
        if item.is_dir() and not item.is_symlink():
            item.chmod(0o700)
    shutil.rmtree(path, ignore_errors=True)


def _remove(docker: str, name: str) -> None:
    try:
        _docker(docker, "rm", "-f", name, timeout=DOCKER_CALL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        pass  # sandbox cleanup retries later from the label


def execute(files: list[dict], profile_name: str, image_id: str, *, run_id: str | None = None,
            limits: Limits = DEFAULT_LIMITS, on_started=None) -> SandboxResult:
    """Run the profile's fixed command against `files` inside a disposable container."""
    profile = PROFILES.get(profile_name)
    if profile is None:
        raise SandboxRefused("Profil de test inconnu")
    run_id = run_id or new_id()
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise SandboxRefused("Identifiant d'exécution invalide")
    docker = docker_binary()
    name = NAME_PREFIX + run_id
    base = workspace_base()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    workspace = base / run_id
    started = time.monotonic()
    sinks = (_Sink(limits.stored_bytes_per_stream), _Sink(limits.stored_bytes_per_stream))

    def result(state, exit_code=None, detail=""):
        total = sinks[0].seen + sinks[1].seen
        return SandboxResult(state=state, exit_code=exit_code,
                             duration_ms=round((time.monotonic() - started) * 1000),
                             stdout=sinks[0].text(), stderr=sinks[1].text(), output_bytes=total,
                             truncated=any(s.seen > len(s.data) for s in sinks),
                             detail=detail[:250], container_name=name)
    try:
        # Fresh directory, regular files only; same path rules as generation.
        workspace.mkdir(mode=0o700)
        safe_files(workspace, files)
        for path in [workspace, *workspace.rglob("*")]:
            # Readable by the unprivileged container user; never writable.
            path.chmod(0o555 if path.is_dir() else 0o444)
        deadline = int(time.time()) + limits.timeout_s + CLEANUP_GRACE_S
        create = _docker(docker, *docker_create_argv(docker, name, workspace, profile, image_id, limits,
                                                     deadline)[1:])
        if create.returncode != 0:
            return result("ERROR", detail="docker create refusé : image absente/non autorisée ou démon indisponible")
        process = subprocess.Popen([docker, "start", "--attach", name], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        overflow = threading.Event()

        def pump(stream, sink):
            for chunk in iter(lambda: stream.read1(65536), b""):
                sink.feed(chunk)
                if sinks[0].seen + sinks[1].seen > limits.max_output_bytes:
                    overflow.set()
        pumps = [threading.Thread(target=pump, args=(process.stdout, sinks[0]), daemon=True),
                 threading.Thread(target=pump, args=(process.stderr, sinks[1]), daemon=True)]
        for t in pumps:
            t.start()
        if on_started is not None:
            on_started(name)
        reason = None
        limit_at = time.monotonic() + limits.timeout_s
        while process.poll() is None:
            if overflow.is_set():
                reason = "OUTPUT_LIMIT"
            elif time.monotonic() >= limit_at:
                reason = "TIMEOUT"
            if reason:
                # SIGKILL: a process ignoring SIGTERM cannot delay the stop.
                try:
                    _docker(docker, "kill", "--signal", "KILL", name)
                except (OSError, subprocess.SubprocessError):
                    pass
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
            time.sleep(0.05)
        for t in pumps:
            t.join(timeout=10)
        state_raw = _docker(docker, "inspect", "--format", "{{json .State}}", name)
        state = json.loads(state_raw.stdout or b"{}") if state_raw.returncode == 0 else {}
        exit_code = state.get("ExitCode")
        if reason:
            return result(reason, exit_code, "limite de durée atteinte" if reason == "TIMEOUT"
                          else "sortie excessive : conteneur arrêté")
        if state.get("OOMKilled"):
            return result("OOM", exit_code, "limite mémoire atteinte")
        if exit_code is None:
            return result("ERROR", None, "état du conteneur indisponible")
        return result("PASSED" if exit_code == 0 else "FAILED", exit_code)
    except (OSError, subprocess.SubprocessError) as exc:
        return result("ERROR", None, f"erreur d'exécution Docker ({type(exc).__name__})")
    finally:
        _remove(docker, name)
        if workspace.exists():
            _discard_workspace(workspace)


# --------------------------------------------------------------------------
# Approbation dédiée, exécution à usage unique et traçabilité SQL


def _lock(db, task_id: str):
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    else:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 7109247))"), {"k": task_id})


def _generated_manifest(db, task_id: str) -> list[dict]:
    task = db.get(Task, task_id)
    if task is None or task.category != "prototype":
        raise SandboxRefused("Tâche prototype inconnue")
    if not db.scalar(select(Approval.id).where(Approval.task_id == task_id)):
        raise SandboxRefused("Prototype jamais approuvé pour génération")
    if task.status not in ("awaiting_evaluation", "completed") or not task.artifact_manifest:
        raise SandboxRefused("Prototype non généré : rien à tester")
    return json.loads(task.artifact_manifest)["files"]


def approve_execution(sessions, task_id: str, *, profile: str, image_id: str, actor: str, reason: str) -> str:
    """Record a human, single-use authorization bound to the exact content and image."""
    if profile not in PROFILES:
        raise SandboxRefused("Profil de test inconnu")
    if not IMAGE_ID.match(image_id):
        raise SandboxRefused("Image non identifiée par un ID sha256")
    actor, reason = actor.strip(), reason.strip()
    if not actor or len(actor) > 100 or not actor.isprintable():
        raise SandboxRefused("Identité humaine requise (100 caractères maximum)")
    if not reason or len(reason) > 500:
        raise SandboxRefused("Motif de l'autorisation requis (500 caractères maximum)")
    with sessions.begin() as db:
        _lock(db, task_id)
        digest = manifest_digest(_generated_manifest(db, task_id))
        approval = ExecutionApproval(task_id=task_id, manifest_sha256=digest, profile=profile,
                                     image_id=image_id, actor=actor, reason=reason)
        db.add(approval)
        db.flush()
        db.add(AuditEvent(entity_id=approval.id, kind="execution_approved"))
        return approval.id


def run_approved(sessions, task_id: str, *, images: dict[str, str] | None = None,
                 limits: Limits = DEFAULT_LIMITS, executor=execute, on_started=None) -> SandboxRun:
    """Consume one matching approval, execute once, persist bounded results."""
    images = allowed_images() if images is None else images
    run_id = new_id()
    with sessions.begin() as db:
        _lock(db, task_id)
        files = _generated_manifest(db, task_id)
        digest = manifest_digest(files)
        candidates = db.scalars(select(ExecutionApproval).where(
            ExecutionApproval.task_id == task_id, ExecutionApproval.consumed_at.is_(None)
        ).order_by(ExecutionApproval.created_at, ExecutionApproval.id)).all()
        if not candidates:
            raise SandboxRefused("Aucune autorisation d'exécution disponible pour cette tâche")
        approval = next((a for a in candidates if a.manifest_sha256 == digest), None)
        if approval is None:
            raise SandboxRefused("Le contenu a changé depuis l'autorisation : nouvelle approbation requise")
        if images.get(approval.profile) != approval.image_id:
            raise SandboxRefused("Image autorisée différente de la configuration courante : aucune exécution")
        approval.consumed_at = utcnow()
        attempt = (db.scalar(select(func.max(SandboxRun.attempt)).where(SandboxRun.task_id == task_id)) or 0) + 1
        run = SandboxRun(id=run_id, task_id=task_id, approval_id=approval.id, attempt=attempt,
                         profile=approval.profile, image_id=approval.image_id, manifest_sha256=digest,
                         container_name=NAME_PREFIX + run_id, timeout_s=limits.timeout_s, state="RUNNING",
                         stdout="", stderr="", output_bytes=0, truncated=False, detail="")
        db.add(run)
        db.add(AuditEvent(entity_id=run_id, kind="sandbox_started"))
        profile, image_id = approval.profile, approval.image_id
    try:
        outcome = executor(files, profile, image_id, run_id=run_id, limits=limits, on_started=on_started)
    except SandboxRefused as exc:
        outcome = SandboxResult("ERROR", None, 0, "", "", 0, False, str(exc)[:250])
    except Exception as exc:  # recorded, never retried: the approval stays consumed
        outcome = SandboxResult("ERROR", None, 0, "", "", 0, False, f"erreur interne ({type(exc).__name__})")
    with sessions.begin() as db:
        run = db.get(SandboxRun, run_id)
        if run.state == "RUNNING":
            run.state, run.exit_code = outcome.state, outcome.exit_code
            run.duration_ms, run.output_bytes, run.truncated = outcome.duration_ms, outcome.output_bytes, outcome.truncated
            run.stdout, run.stderr, run.detail = outcome.stdout, outcome.stderr, outcome.detail
            run.finished_at = utcnow()
            db.add(AuditEvent(entity_id=run_id, kind=f"sandbox_{outcome.state.lower()}"))
        db.flush()
        db.expunge(run)
    return run


def cleanup(sessions=None, *, force: bool = False, now: float | None = None) -> dict:
    """Remove leftover labelled containers/workspaces; mark stale RUNNING records INTERRUPTED.

    Without `force`, only containers past their deadline label are removed. `force`
    removes every sandbox container: use only when no sandbox run is in progress.
    """
    docker, now = docker_binary(), time.time() if now is None else now
    removed = []
    listing = _docker(docker, "ps", "--all", "--filter", f"label={LABEL}=1",
                      "--format", '{{.Names}}\t{{.Label "aac.deadline"}}')
    if listing.returncode != 0:
        raise SandboxError("Liste des conteneurs Docker indisponible")
    for line in listing.stdout.decode().splitlines():
        name, _, deadline = line.partition("\t")
        if not name.startswith(NAME_PREFIX):
            continue
        if force or not deadline.isdigit() or int(deadline) <= now:
            _remove(docker, name)
            removed.append(name)
    interrupted = []
    if sessions is not None:
        moment = datetime.fromtimestamp(now, timezone.utc)
        with sessions.begin() as db:
            for run in db.scalars(select(SandboxRun).where(SandboxRun.state == "RUNNING")).all():
                started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
                if force or started + timedelta(seconds=run.timeout_s + CLEANUP_GRACE_S) <= moment:
                    run.state, run.finished_at = "INTERRUPTED", utcnow()
                    run.detail = "exécution interrompue : approbation consommée, non rejouée"
                    db.add(AuditEvent(entity_id=run.id, kind="sandbox_interrupted"))
                    interrupted.append(run.id)
    base = workspace_base()
    if base.is_dir():
        stale = {name[len(NAME_PREFIX):] for name in removed} | set(interrupted)
        for path in base.iterdir():
            if force or path.name in stale:
                _discard_workspace(path)
    return {"containers_removed": removed, "runs_interrupted": interrupted}
