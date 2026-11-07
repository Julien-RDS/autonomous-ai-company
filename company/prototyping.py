"""Génère des fichiers de prototype SANS exécuter le code produit."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from sqlalchemy import select, text
from pathlib import Path

from .agents import AgentService
from .config import ROOT
from .database import Run, Task, Approval
from .routing import select_provider
from .workflow import parse_object

ALLOWED = {".md", ".txt", ".html", ".css", ".js", ".json", ".py"}


def safe_files(root: Path, files: list[dict]) -> list[Path]:
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise ValueError("De 1 à 8 fichiers sont autorisés")
    root.mkdir(parents=True, exist_ok=True)
    checked = []
    for spec in files:
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not isinstance(spec.get("content"), str):
            raise ValueError("Fichier JSON incorrect")
        rel = Path(spec["path"])
        if rel.is_absolute() or ".." in rel.parts or rel.suffix.lower() not in ALLOWED:
            raise ValueError("Chemin ou extension interdit(e)")
        if len(spec["content"].encode("utf-8")) > 64_000:
            raise ValueError("Fichier trop volumineux")
        destination = (root / rel).resolve()
        if not destination.is_relative_to(root.resolve()):
            raise ValueError("Sortie du répertoire de travail refusée")
        if destination in checked or destination.exists():
            raise ValueError("Doublon ou fichier déjà existant")
        checked.append(destination)
    for spec, destination in zip(files, checked):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(spec["content"], encoding="utf-8")
    return checked


def _generate_prototype(sessions, settings, budget, task_id: str, lease=None) -> tuple[str, list[Path]]:
    with sessions() as db:
        task = db.get(Task, task_id)
        if task is None or task.category != "prototype" or task.status not in ("approved", "awaiting_evaluation", "completed"):
            raise ValueError("Une tâche 'prototype' doit d'abord être approuvée")
        if not db.scalar(select(Approval.id).where(Approval.task_id == task_id)):
            raise ValueError("Validation humaine enregistrée requise")
        run = db.get(Run, task.run_id)
        if run is None or run.status != "completed":
            raise ValueError("Réunion source introuvable/incomplète")
        if run.mode != ("mock" if settings.mock else "real"):
            raise ValueError("Mode incompatible avec la réunion source")
        selection = json.loads(run.selection_info)
        run_id = run.id
        manifest = json.loads(task.artifact_manifest) if task.artifact_manifest else None
        provider = task.provider or select_provider(sessions, category="prototype")
        if task.status != "approved" and manifest is None:
            raise ValueError("Prototype sans manifeste récupérable")
    with sessions.begin() as db:
        db.get(Task, task_id).provider = provider
    proposal = selection["proposal"]
    prompt = f"""Créer un MINI-PROTOTYPE STATIC et LOCAL pour : {proposal['title']}.
Contexte : {proposal['problem']}. Cible présumée : {proposal['customer']}.
Il ne faut pas simuler une validation de marché, de vrais clients ou des revenus.
Répondre UNIQUEMENT au format JSON : {{"files":[{{"path":"index.html", "content":"..."}}, ...]}}.
1 à 8 fichiers, moins de 64 Ko par fichier, extensions html, css, js, md, json, py ou txt.
ZÉRO dépendance externe, ZÉRO requête réseau, ZÉRO secret, ZÉRO exécution automatique.
Le code produit sera vérifié avant tout lancement ; privilégie un index.html autonome et un README.md."""
    root = ROOT / "artifacts" / "prototypes" / task_id
    root.parent.mkdir(parents=True, exist_ok=True)
    if manifest is None:
        content = AgentService(sessions, settings, budget).call(provider, "prototype", prompt, run_id)
        files = parse_object(content).get("files", [])
        # Validate everything in a private staging directory before recording intent.
        with tempfile.TemporaryDirectory(prefix=".prototype-", dir=root.parent) as staging:
            safe_files(Path(staging), files)
        files = [{'path': Path(spec['path']).as_posix(), 'content': spec['content']} for spec in files]
        manifest = {"provider": provider, "files": files}
        with sessions.begin() as db:
            db.get(Task, task_id).artifact_manifest = json.dumps(manifest, ensure_ascii=False)
    files = manifest['files']
    with sessions.begin() as db:
        if lease is not None:
            from .queue import Queue
            Queue(sessions)._owned(db, *lease)
        if root.exists() or root.is_symlink():
            verify_artifacts(root, files)
        else:
            staging = Path(tempfile.mkdtemp(prefix=".prototype-", dir=root.parent))
            try:
                safe_files(staging, files)
                os.rename(staging, root)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        task = db.get(Task, task_id)
        if task.status == "approved":
            task.status = "awaiting_evaluation"
    return manifest['provider'], [root / spec['path'] for spec in files]


def verify_artifacts(root, files):
    if root.is_symlink():
        raise ValueError("Répertoire prototype symbolique interdit")
    expected = {spec['path']: spec['content'] for spec in files}
    actual = {}
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError("Lien symbolique interdit dans un prototype existant")
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = path.read_text(encoding='utf-8')
    if actual != expected:
        raise ValueError("Artefacts existants différents du manifeste; aucune réécriture")


@contextmanager
def prototype_lock(sessions, task_id):
    engine = sessions.kw['bind']
    if engine.dialect.name == 'postgresql':
        with engine.connect() as connection:
            locked = connection.scalar(text(
                'SELECT pg_try_advisory_lock(hashtextextended(:key, 7109246))'
            ), {'key': task_id})
            if not locked:
                raise RuntimeError('Prototype déjà détenu par un autre processus')
            try:
                yield
            finally:
                connection.execute(text('SELECT pg_advisory_unlock(hashtextextended(:key, 7109246))'), {'key': task_id})
    else:
        import fcntl
        directory = ROOT / 'artifacts' / 'prototypes' / '.locks'
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f'{task_id}.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Prototype déjà détenu par un autre processus') from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def generate_prototype(sessions, settings, budget, task_id: str, *, lease=None):
    # IDs originate in the database; reject path injection before opening a lock file.
    if not task_id or len(task_id) > 40 or not all(c in '0123456789abcdef' for c in task_id):
        raise ValueError('Identifiant de tâche invalide')
    with prototype_lock(sessions, task_id):
        return _generate_prototype(sessions, settings, budget, task_id, lease)
