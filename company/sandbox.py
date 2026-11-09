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
import stat
import unicodedata
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select, text

from . import prototyping
from .database import Approval, AuditEvent, ExecutionApproval, SandboxCleanup, SandboxRun, SandboxEvidence, Task, new_id, utcnow
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
        argv=('python','-I','-B','-c',Path(__file__).with_name('unittest_runner.py').read_text(encoding='utf-8')),
        env=(("HOME", "/tmp"), ("PYTHONDONTWRITEBYTECODE", "1"), ("LANG", "C.UTF-8")),
    ),
}

PROFILES['static-html'] = Profile(
    name='static-html', argv=('python','-I','-B','-c',
        Path(__file__).with_name('static_validation.py').read_text(encoding='utf-8')+"\nimport sys\nprint('AAC_RESULT_V1:{\"kind\":\"static\",\"structure_valid\":true}',file=sys.stderr)\n"),
    env=PROFILES['python-unittest'].env,
)


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
    # Never resolve symlinks silently: a link planted in a shared /tmp would
    # redirect the workspaces to a directory controlled by someone else.
    base = Path(os.path.abspath(os.getenv("SANDBOX_WORKDIR") or Path(tempfile.gettempdir()) / "aac-sandbox"))
    # The path is embedded in a --mount specification: refuse its separators.
    if any(c in str(base) for c in ",=\n\"'"):
        raise SandboxRefused("SANDBOX_WORKDIR contient un caractère interdit")
    return base


def secure_base(create: bool = True) -> Path | None:
    """Workspace root must be a real directory, owned by us, mode 0700, without symlink in its path."""
    base = workspace_base()
    try:
        info = os.lstat(base)
    except FileNotFoundError:
        if not create:
            return None
        base.mkdir(mode=0o700, parents=True)
        info = os.lstat(base)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or os.path.realpath(base) != str(base):
        raise SandboxRefused("SANDBOX_WORKDIR doit être un répertoire réel, sans lien symbolique")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SandboxRefused("SANDBOX_WORKDIR doit appartenir à l'utilisateur courant avec des droits 0700")
    return base


def allowed_images() -> dict[str, str]:
    """Explicit allowlist: profile -> immutable local image ID (sha256:...)."""
    image = os.getenv("SANDBOX_PYTHON_IMAGE_ID", "").strip()
    if not image:
        return {}
    if not IMAGE_ID.match(image):
        raise SandboxRefused("SANDBOX_PYTHON_IMAGE_ID doit être un identifiant sha256:<64 hex>")
    return {name: image for name in PROFILES}


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
    # Docker CLI may inject proxy variables from its config; never expose those.
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','FTP_PROXY','NO_PROXY',
                'http_proxy','https_proxy','all_proxy','ftp_proxy','no_proxy'):
        argv += ['--env', f'{key}=']
    argv += ['--entrypoint','python']
    for key, value in profile.env:
        argv += ["--env", f"{key}={value}"]
    return argv + [image_id, *profile.argv[1:]]


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


# JSON keys of `docker info --format "{{json .}}"` (note: CpuCfsQuota, not the Go field name).
REQUIRED_CAPABILITIES = ("MemoryLimit", "SwapLimit", "PidsLimit", "CpuCfsQuota")


def docker_capabilities(docker: str | None = None) -> dict:
    """What the daemon declares. Declared support is necessary, not sufficient."""
    docker = docker or docker_binary()
    try:
        info = _docker(docker, "info", "--format", "{{json .}}")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"reachable": False, "missing": list(REQUIRED_CAPABILITIES), "error": type(exc).__name__}
    if info.returncode != 0:
        return {"reachable": False, "missing": list(REQUIRED_CAPABILITIES), "error": "démon Docker injoignable"}
    try:
        data = json.loads(info.stdout or b"{}")
    except ValueError:
        data = {}
    security = " ".join(data.get("SecurityOptions") or [])
    return {"reachable": True, "missing": [k for k in REQUIRED_CAPABILITIES if data.get(k) is not True],
            "cgroup_version": data.get("CgroupVersion"), "cgroup_driver": data.get("CgroupDriver"),
            "rootless": "rootless" in security, "server_version": data.get("ServerVersion"), "error": None}


def resource_limits_supported(docker: str) -> bool:
    caps = docker_capabilities(docker)
    return caps["reachable"] and not caps["missing"]


# Fixed application probe (never user content). It locates its OWN cgroup through
# /proc/self/cgroup, so it works with cgroup v1, v2 with a private cgroup
# namespace (paths at the root) and v2 with the host namespace (nested paths).
PROBE_SCRIPT = r"""
import json, os
def read(path):
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return None
own = {}
for line in (read('/proc/self/cgroup') or '').splitlines():
    parts = line.split(':', 2)
    if len(parts) == 3:
        for controller in (parts[1].split(',') if parts[1] else ['']):
            own[controller] = parts[2].strip('/')
def first(candidates):
    for path in candidates:
        value = read(path)
        if value is not None:
            return value
    return None
def v2(name):
    nested = own.get('', '')
    return first(([f'/sys/fs/cgroup/{nested}/{name}'] if nested else []) + [f'/sys/fs/cgroup/{name}'])
def v1(controller, directories, name):
    nested = own.get(controller, '')
    paths = []
    for directory in directories:
        if nested:
            paths.append(f'/sys/fs/cgroup/{directory}/{nested}/{name}')
        paths.append(f'/sys/fs/cgroup/{directory}/{name}')
    return first(paths)
cpu_dirs = ['cpu', 'cpu,cpuacct', 'cpuacct,cpu']
print(json.dumps({
    'memory': v2('memory.max') or v1('memory', ['memory'], 'memory.limit_in_bytes'),
    'swap_v2': v2('memory.swap.max'),
    'memsw_v1': v1('memory', ['memory'], 'memory.memsw.limit_in_bytes'),
    'pids': v2('pids.max') or v1('pids', ['pids'], 'pids.max'),
    'cpu_v2': v2('cpu.max'),
    'quota_v1': v1('cpu', cpu_dirs, 'cpu.cfs_quota_us'),
    'period_v1': v1('cpu', cpu_dirs, 'cpu.cfs_period_us'),
    'uid': os.getuid(),
    'cgroup': (read('/proc/self/cgroup') or '')[:200],
}))
"""
PROBE_PROFILE = Profile(name="limits-probe", argv=("python", "-I", "-B", "-c", PROBE_SCRIPT),
                        env=(("HOME", "/tmp"), ("LANG", "C.UTF-8")))


def _bytes(size: str) -> int:
    match = re.fullmatch(r"(\d+)([kmg]?)", size.strip().lower())
    if not match:
        raise ValueError("Taille mémoire invalide")
    return int(match.group(1)) * {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match.group(2)]


def limits_mismatch(observed: dict, limits: Limits) -> list[str]:
    """Compare kernel cgroup values seen INSIDE the container with what was requested."""
    problems = []
    memory = _bytes(limits.memory)
    if observed.get("memory") != str(memory):
        problems.append("mémoire")
    if observed.get("swap_v2") is not None:
        if observed.get("swap_v2") != "0":
            problems.append("swap")
    elif observed.get("memsw_v1") != str(memory):
        problems.append("swap")
    if observed.get("pids") != str(limits.pids):
        problems.append("processus")
    cpu = observed.get("cpu_v2")
    quota, period = (cpu.split() + [""])[:2] if cpu else (observed.get("quota_v1"), observed.get("period_v1"))
    try:
        if abs(int(quota) / int(period) - float(limits.cpus)) > 0.01:
            problems.append("CPU")
    except (TypeError, ValueError, ZeroDivisionError):
        problems.append("CPU")
    if observed.get("uid") in (None, 0):
        problems.append("utilisateur non root")
    return problems


def describe_observed(observed: dict) -> str:
    keys = ("memory", "swap_v2", "memsw_v1", "pids", "cpu_v2", "quota_v1", "period_v1", "uid")
    return " ".join(f"{k}={str(observed.get(k))[:24]}" for k in keys if observed.get(k) is not None) or "aucune valeur"


def probe_enforcement(docker: str, workspace: Path, image_id: str, limits: Limits, run_id: str,
                      details: dict | None = None) -> list[str]:
    """Run a fixed application probe with the SAME options and read the effective limits."""
    name = f"{NAME_PREFIX}{run_id}-probe"
    deadline = int(time.time()) + 30 + CLEANUP_GRACE_S
    owned = False
    try:
        created = _docker(docker, *docker_create_argv(docker, name, workspace, PROBE_PROFILE, image_id,
                                                      limits, deadline)[1:])
        if created.returncode != 0:
            return ["conteneur de vérification non créé"]
        owned = True
        started = _docker(docker, "start", "--attach", name, timeout=30)
        if started.returncode != 0:
            return ["vérification des limites impossible"]
        observed = json.loads(started.stdout.decode().strip().splitlines()[-1])
        if not isinstance(observed, dict):
            return ["vérification des limites impossible"]
        if details is not None:
            details.update(observed)
        return limits_mismatch(observed, limits)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return ["vérification des limites impossible"]
    finally:
        if owned:
            _remove(docker, name)


def check_limits(image_id: str, limits: Limits = DEFAULT_LIMITS) -> dict:
    """Diagnostic: run only the fixed probe in an empty workspace and report kernel values."""
    if not IMAGE_ID.match(image_id):
        raise SandboxRefused("Image non identifiée par un ID sha256")
    docker, run_id = docker_binary(), new_id()
    workspace = secure_base() / run_id
    workspace.mkdir(mode=0o700)
    try:
        workspace.chmod(0o555)
        observed: dict = {}
        problems = probe_enforcement(docker, workspace, image_id, limits, run_id, observed)
        return {"capabilities": docker_capabilities(docker), "observed": observed, "problems": problems}
    finally:
        _discard_workspace(workspace)


def execution_verdict(exit_code, stderr, profile, *, truncated=False):
    from .sandbox_summary import parse_summary
    if exit_code != 0:
        return 'FAILED'
    return 'PASSED' if parse_summary(stderr,profile,truncated=truncated) is not None else 'ERROR'


def display_safe(text: str) -> str:
    """Untrusted output for a terminal: neutralize escape/control characters (ANSI, OSC...)."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C")
                   else f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}" for c in text)


@contextmanager
def lifecycle_lock(run_id):
    """One supervisor host per Docker daemon; kernel releases locks on parent death."""
    import fcntl
    directory=Path(tempfile.gettempdir()) / f'aac-lifecycle-{os.geteuid()}'
    directory.mkdir(mode=0o700, exist_ok=True)
    info=directory.lstat()
    if directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SandboxRefused('Répertoire des verrous non sûr')
    key=hashlib.sha256((str(workspace_base())+':'+run_id).encode()).hexdigest()
    fd=os.open(directory/key,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise SandboxRefused('Superviseur encore actif : nettoyage ou exécution concurrente refusé') from None
        try:
            yield
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)


def execute(files, profile_name, image_id, *, run_id=None, **kwargs):
    run_id=run_id or new_id()
    if not re.fullmatch(r'[0-9a-f]{32}',run_id):
        raise SandboxRefused('Identifiant invalide')
    with lifecycle_lock(run_id):
        return _execute(files,profile_name,image_id,run_id=run_id,**kwargs)


def _execute(files: list[dict], profile_name: str, image_id: str, *, run_id: str | None = None,
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
    base = secure_base()
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
    workspace_created = container_created = False
    try:
        if not resource_limits_supported(docker):
            return result('ERROR', detail='Docker ne peut pas imposer les limites mémoire/CPU/PID; exécution refusée')
        # Fresh directory, regular files only; same path rules as generation.
        workspace.mkdir(mode=0o700)
        workspace_created = True
        safe_files(workspace, files)
        for path in [workspace, *workspace.rglob("*")]:
            # Readable by the unprivileged container user; never writable.
            path.chmod(0o555 if path.is_dir() else 0o444)
        observed: dict = {}
        missing = probe_enforcement(docker, workspace, image_id, limits, run_id, observed)
        if missing:
            # An accepted Docker option is not proof: the kernel values did not match.
            return result("ERROR", detail="limites non appliquées par le noyau (" + ", ".join(missing)
                          + ") ; exécution refusée ; observé : " + describe_observed(observed))
        deadline = int(time.time()) + limits.timeout_s + CLEANUP_GRACE_S
        create = _docker(docker, *docker_create_argv(docker, name, workspace, profile, image_id, limits,
                                                     deadline)[1:])
        if create.returncode != 0:
            return result("ERROR", detail="docker create refusé : image absente/non autorisée ou démon indisponible")
        container_created = True
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
        verdict=execution_verdict(exit_code,sinks[1].text(),profile_name,
                                  truncated=any(s.seen>len(s.data) for s in sinks))
        return result(verdict,exit_code,'résumé absent ou invalide' if verdict=='ERROR' else '')
    except (OSError, subprocess.SubprocessError) as exc:
        return result("ERROR", None, f"erreur d'exécution Docker ({type(exc).__name__})")
    finally:
        if container_created:
            _remove(docker, name)
        if workspace_created and workspace.exists():
            try:
                remaining=_containers(docker)
                if name not in remaining and name+'-probe' not in remaining:
                    _discard_workspace(workspace)
            except (SandboxError, OSError, subprocess.SubprocessError):
                pass  # Unknown daemon state: preserve files for audited recovery.


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


def validator_version(profile):
    return hashlib.sha256(json.dumps(PROFILES[profile].argv).encode()).hexdigest()


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
        files = _generated_manifest(db, task_id)
        reviewed = prototyping.ROOT / "artifacts" / "prototypes" / task_id
        if not reviewed.is_dir():
            raise SandboxRefused("Fichiers à relire introuvables sur disque : autorisation impossible")
        try:
            # The human reviews these files; they must be exactly what would run.
            prototyping.verify_artifacts(reviewed, files)
        except (ValueError, OSError, UnicodeDecodeError):
            raise SandboxRefused("Fichiers sur disque différents du contenu qui serait exécuté : "
                                 "relecture invalide, aucune autorisation") from None
        digest = manifest_digest(files)
        approval = ExecutionApproval(validator_version=validator_version(profile),task_id=task_id, manifest_sha256=digest, profile=profile,
                                     image_id=image_id, actor=actor, reason=reason)
        db.add(approval)
        db.flush()
        db.add(AuditEvent(entity_id=approval.id, kind="execution_approved"))
        return approval.id


def preflight(docker: str | None = None) -> None:
    """Refuse BEFORE consuming an approval when this host cannot isolate correctly."""
    secure_base()
    caps = docker_capabilities(docker)
    if not caps["reachable"]:
        raise SandboxRefused("Démon Docker injoignable : aucune autorisation consommée")
    if caps["missing"]:
        raise SandboxRefused("Docker ne peut pas imposer : " + ", ".join(caps["missing"])
                             + " ; aucune autorisation consommée (voir sandbox doctor)")


def run_approved(sessions, task_id: str, *, images: dict[str, str] | None = None,
                 limits: Limits = DEFAULT_LIMITS, executor=execute, on_started=None,
                 check=None) -> SandboxRun:
    """Consume one matching approval, execute once, persist bounded results."""
    images = allowed_images() if images is None else images
    if check is None and executor is execute:
        check = preflight
    if check is not None:
        check()
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
        matching=[a for a in candidates if a.manifest_sha256 == digest]
        approval=next((a for a in matching if a.profile in PROFILES and
                       a.validator_version == validator_version(a.profile)), matching[0] if matching else None)
        if approval is None:
            raise SandboxRefused("Le contenu a changé depuis l'autorisation : nouvelle approbation requise")
        if images.get(approval.profile) != approval.image_id:
            raise SandboxRefused("Image autorisée différente de la configuration courante : aucune exécution")
        if approval.profile not in PROFILES or approval.validator_version != validator_version(approval.profile):
            raise SandboxRefused('Validateur modifié ou historique : nouvelle approbation requise')
        approval.consumed_at = utcnow()
        attempt = (db.scalar(select(func.max(SandboxRun.attempt)).where(SandboxRun.task_id == task_id)) or 0) + 1
        run = SandboxRun(validator_version=validator_version(approval.profile),id=run_id, task_id=task_id, approval_id=approval.id, attempt=attempt,
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
        if db.get_bind().dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        run = db.scalar(select(SandboxRun).where(SandboxRun.id == run_id).with_for_update())
        from .sandbox_summary import parse_summary
        summary=parse_summary(outcome.stderr,profile,truncated=outcome.truncated)
        evidence=db.scalar(select(SandboxEvidence).where(SandboxEvidence.run_id==run_id))
        if evidence is not None:
            db.expunge(run)
            return run
        # The immutable envelope is bounded and retained even when cleanup wins the race.
        envelope=asdict(outcome)
        envelope['stdout']=outcome.stdout[:65536]; envelope['stderr']=outcome.stderr[:65536]
        envelope['detail']=outcome.detail[:250]
        db.add(SandboxEvidence(run_id=run_id,outcome_json=json.dumps(envelope,ensure_ascii=True)))
        run.summary_json=json.dumps(summary) if summary is not None else None
        if run.state == "RUNNING":
            run.state, run.exit_code = outcome.state, outcome.exit_code
            run.duration_ms, run.output_bytes, run.truncated = outcome.duration_ms, outcome.output_bytes, outcome.truncated
            run.stdout, run.stderr, run.detail = outcome.stdout, outcome.stderr, outcome.detail
            run.finished_at = utcnow()
            db.add(AuditEvent(entity_id=run_id, kind=f"sandbox_{outcome.state.lower()}"))
        else:
            # Marked INTERRUPTED meanwhile: keep that verdict, but never drop the evidence.
            run.exit_code = outcome.exit_code if run.exit_code is None else run.exit_code
            run.duration_ms = outcome.duration_ms if run.duration_ms is None else run.duration_ms
            run.output_bytes, run.truncated = outcome.output_bytes, outcome.truncated
            run.stdout = run.stdout or outcome.stdout
            run.stderr = run.stderr or outcome.stderr
            run.detail = f"{run.detail[:120]} | résultat tardif : {outcome.state} {outcome.detail}"[:250]
            db.add(AuditEvent(entity_id=run_id, kind="sandbox_late_result"))
        db.flush()
        db.expunge(run)
    return run


def _record_cleanup(db, *, run_id, container, actor, reason, previous, outcome, container_removed,
                    workspace_removed):
    entry = SandboxCleanup(run_id=run_id, container_name=container, actor=actor, reason=reason[:500],
                           previous_state=previous, outcome=outcome, container_removed=container_removed,
                           workspace_removed=workspace_removed)
    db.add(entry)
    db.flush()
    db.add(AuditEvent(entity_id=run_id or entry.id, kind=f"sandbox_cleanup_{outcome}"[:40]))
    return entry.id


def _containers(docker: str) -> dict[str, dict]:
    listing = _docker(docker, "ps", "--all", "--filter", f"label={LABEL}=1",
                      "--format", '{{.Names}}\t{{.Label "aac.deadline"}}\t{{.State}}')
    if listing.returncode != 0:
        raise SandboxError("Liste des conteneurs Docker indisponible")
    found = {}
    for line in listing.stdout.decode().splitlines():
        name, deadline, state = (line.split("\t") + ["", ""])[:3]
        if name.startswith(NAME_PREFIX):
            found[name] = {"deadline": int(deadline) if deadline.isdigit() else None, "state": state}
    return found


# A container that is created/starting/restarting may belong to a live parent.
LIVE_STATES = ("created", "running", "restarting")


def _run_of(container: str) -> str | None:
    core = container[len(NAME_PREFIX):].removesuffix("-probe")
    return core if re.fullmatch(r"[0-9a-f]{32}", core) else None


def cleanup(sessions=None, *, force=False, now=None, actor='cli:cleanup',
            reason='nettoyage des orphelins échus'):
    docker= docker_binary()
    now=time.time() if now is None else now
    listing=_containers(docker)
    removed=[]; interrupted=[]
    known=set()
    if sessions is not None:
        base=secure_base(create=False)
        workspaces={p.name for p in base.iterdir()} if base is not None else set()
        with sessions() as db:
            known=set(db.scalars(select(SandboxRun.id)).all())
            rows=db.scalars(select(SandboxRun).where(
                (SandboxRun.state=='RUNNING') | SandboxRun.container_name.in_(listing) |
                SandboxRun.id.in_(workspaces))).all()
        for run in rows:
            started=run.started_at.replace(tzinfo=timezone.utc)
            due=started.timestamp()+run.timeout_s+CLEANUP_GRACE_S <= now
            if not force and not due:
                continue
            try:
                report=cleanup_run(sessions,run.id,actor=actor,reason=reason,now=now,force=force)
            except SandboxRefused:
                continue
            removed.extend(report['containers_removed'])
            if report['outcome']=='interrupted': interrupted.append(run.id)
    for name,info in listing.items():
        run_id=_run_of(name)
        if run_id is None or run_id in known or (not force and (info['deadline'] is None or info['deadline']>now)):
            continue
        try:
            with lifecycle_lock(run_id):
                intent_id=None
                if sessions is not None:
                    with sessions.begin() as db:
                        intent_id=_record_cleanup(db,run_id=None,container=name,actor=actor,reason=reason,
                            previous=None,outcome='started',container_removed=False,workspace_removed=False)
                _remove(docker,name)
                # Unknown state or remaining probe means files are never deleted.
                still=_containers(docker)
                if name not in still: removed.append(name)
                if not any(_run_of(n)==run_id for n in still):
                    base=secure_base(create=False)
                    if base is not None: _discard_workspace(base/run_id)
                if intent_id is not None:
                    with sessions.begin() as db:
                        entry=db.get(SandboxCleanup,intent_id)
                        entry.container_removed=name not in still
                        entry.outcome='container_removed' if name not in still else 'cleanup_failed'
                        db.add(AuditEvent(entity_id=entry.id,kind='sandbox_cleanup_'+entry.outcome))
        except (SandboxRefused,SandboxError,OSError,subprocess.SubprocessError):
            continue
    return {'containers_removed':removed,'runs_interrupted':interrupted}


def diagnose(sessions, *, now: float | None = None) -> dict:
    """Read-only: Docker capabilities and the status of RUNNING runs and labelled containers."""
    docker, now = docker_binary(), time.time() if now is None else now
    caps = docker_capabilities(docker)
    containers = _containers(docker) if caps["reachable"] else {}
    with sessions() as db:
        running = db.scalars(select(SandboxRun).where(SandboxRun.state == "RUNNING")
                             .order_by(SandboxRun.started_at)).all()
        known = set(db.scalars(select(SandboxRun.id)).all())
    runs = []
    for run in running:
        started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
        deadline = started.timestamp() + run.timeout_s + CLEANUP_GRACE_S
        container = containers.get(run.container_name)
        if not caps["reachable"]:
            status = "inconnu (Docker injoignable)"
        elif deadline <= now:
            status = "orphelin : échéance dépassée"
        elif container is None:
            status = "orphelin : conteneur absent"
        elif container["state"] not in LIVE_STATES:
            status = "orphelin : conteneur arrêté"
        else:
            status = "actif"
        runs.append({"run_id": run.id, "task_id": run.task_id, "attempt": run.attempt, "status": status,
                     "container": container["state"] if container else None})
    unknown = [n for n in containers if _run_of(n) not in known]
    return {"capabilities": caps, "running": runs, "unknown_containers": unknown}


def _cleanup_run(sessions, run_id: str, *, actor: str, reason: str, now: float | None = None, force: bool = False) -> dict:
    """Targeted cleanup of ONE run: its container(s) and workspace only. Audited, never re-executes."""
    if not re.fullmatch(r"[0-9a-f]{32}", run_id or ""):
        raise SandboxRefused("Identifiant d'exécution invalide")
    actor, reason = actor.strip(), reason.strip()
    if not actor or len(actor) > 100 or not actor.isprintable():
        raise SandboxRefused("Identité humaine requise (100 caractères maximum)")
    if not reason or len(reason) > 500 or not reason.isprintable():
        raise SandboxRefused("Motif requis (500 caractères maximum)")
    docker, now = docker_binary(), time.time() if now is None else now
    with sessions() as db:
        run = db.get(SandboxRun, run_id)
        if run is None:
            raise SandboxRefused("Exécution inconnue")
        previous = run.state
    names = [NAME_PREFIX + run_id, f"{NAME_PREFIX}{run_id}-probe"]
    containers = {n: c for n, c in _containers(docker).items() if n in names}
    active = [n for n, c in containers.items()
              if c["state"] in LIVE_STATES and (c["deadline"] is None or c["deadline"] > now)]
    with sessions() as db:
        current=db.get(SandboxRun,run_id)
        before_deadline=(current.started_at.replace(tzinfo=timezone.utc).timestamp()+current.timeout_s+CLEANUP_GRACE_S)>now
        if current.state=='RUNNING' and before_deadline and not containers:
            active.append('supervisor-starting')
    with sessions.begin() as db:
        if active and not force:
            _record_cleanup(db, run_id=run_id, container=names[0], actor=actor, reason=reason,
                            previous=previous, outcome="refused_active", container_removed=False,
                            workspace_removed=False)
            refused = True
        else:
            refused = False
    if refused:
        raise SandboxRefused("Conteneur encore actif avant échéance : nettoyage refusé, réessayer après l'échéance")
    # Persist intent before touching Docker; a process crash leaves a visible pending action.
    with sessions.begin() as db:
        intent_id=_record_cleanup(db,run_id=run_id,container=names[0],actor=actor,reason=reason,
                                  previous=previous,outcome='started',container_removed=False,workspace_removed=False)
    try:
        for name in containers:
            _remove(docker,name)
        still = set(_containers(docker)) & set(names)
    except (SandboxError,OSError,subprocess.SubprocessError):
        still=set(names)  # Unknown is never interpreted as absent.
    workspace_removed=False
    base=secure_base(create=False)
    if not still and base is not None and (base/run_id).exists():
        _discard_workspace(base/run_id)
        workspace_removed=not (base/run_id).exists()
    files_left=base is not None and (base/run_id).exists()
    with sessions.begin() as db:
        if db.get_bind().dialect.name == 'sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        run=db.scalar(select(SandboxRun).where(SandboxRun.id==run_id).with_for_update())
        outcome=('cleanup_failed' if still or files_left else 'interrupted' if run.state=='RUNNING'
                 else 'cleaned' if containers or workspace_removed else 'nothing_to_clean')
        if not still and not files_left and run.state=='RUNNING':
            run.state='INTERRUPTED'; run.finished_at=utcnow()
            run.detail='nettoyage ciblé confirmé; aucune relance'
        intent=db.get(SandboxCleanup,intent_id)
        intent.outcome=outcome
        intent.container_removed=bool(containers) and not still
        intent.workspace_removed=workspace_removed
        db.add(AuditEvent(entity_id=run_id,kind=f'sandbox_cleanup_{outcome}'))
    return {'run_id':run_id,'previous_state':previous,'outcome':outcome,
            'containers_removed':sorted(set(containers)-still),'workspace_removed':workspace_removed}


def cleanup_run(sessions,run_id,**kwargs):
    if not re.fullmatch(r'[0-9a-f]{32}',run_id or ''):
        raise SandboxRefused('Identifiant invalide')
    with lifecycle_lock(run_id):
        return _cleanup_run(sessions,run_id,**kwargs)
