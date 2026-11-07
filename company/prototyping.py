"""Génère des fichiers de prototype SANS exécuter le code produit."""
from __future__ import annotations

import json
from pathlib import Path

from .agents import AgentService
from .config import ROOT
from .database import Run, Task
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


def generate_prototype(sessions, settings, budget, task_id: str) -> tuple[str, list[Path]]:
    with sessions() as db:
        task = db.get(Task, task_id)
        if task is None or task.category != "prototype" or task.status != "approved":
            raise ValueError("Une tâche 'prototype' doit d'abord être approuvée")
        run = db.get(Run, task.run_id)
        if run is None or run.status != "completed":
            raise ValueError("Réunion source introuvable/incomplète")
        selection = json.loads(run.selection_info)
        run_id = run.id
    provider = select_provider(sessions, category="prototype")
    proposal = selection["proposal"]
    prompt = f"""Créer un MINI-PROTOTYPE STATIC et LOCAL pour : {proposal['title']}.
Contexte : {proposal['problem']}. Cible présumée : {proposal['customer']}.
Il ne faut pas simuler une validation de marché, de vrais clients ou des revenus.
Répondre UNIQUEMENT au format JSON : {{"files":[{{"path":"index.html", "content":"..."}}, ...]}}.
1 à 8 fichiers, moins de 64 Ko par fichier, extensions html, css, js, md, json, py ou txt.
ZÉRO dépendance externe, ZÉRO requête réseau, ZÉRO secret, ZÉRO exécution automatique.
Le code produit sera vérifié avant tout lancement ; privilégie un index.html autonome et un README.md."""
    content = AgentService(sessions, settings, budget).call(provider, "prototype", prompt, run_id)
    parsed = parse_object(content)
    root = ROOT / "artifacts" / "prototypes" / task_id
    paths = safe_files(root, parsed.get("files", []))
    with sessions.begin() as db:
        task = db.get(Task, task_id)
        task.provider = provider
        task.status = "awaiting_evaluation"
    return provider, paths
