"""Commandes de démarrage et d'administration."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from .budget import Budget
from .config import Settings
from .database import Run, Task, session_factory
from .prototyping import generate_prototype
from .routing import rank_providers
from .workflow import Board


def build():
    settings = Settings.load()
    sessions = session_factory(settings.db_url)
    budget = Budget(sessions, settings)
    return settings, sessions, budget


def run_meeting(settings, sessions, budget):
    run_id, decision = Board(sessions, settings, budget).run_once()
    result = decision["decision"]
    print(f"\nRéunion {run_id} — {'DÉMO' if settings.mock else 'RÉEL'}")
    print(f"Décision : {result['proposal']['title']} ({result['votes']}/{result['total_votes']} voix)")
    print(f"Compte rendu : reports/{run_id}.md")
    print(f"Coût comptabilisé : {budget.totals(run_id):.5f} €")
    print("Actions externes non exécutées : consulter / approuver les tâches.")


def main():
    parser = argparse.ArgumentParser(prog="python -m company", description="Entreprise multiagents avec validation humaine")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "once", "status", "ranking", "scheduler", "dashboard"):
        sub.add_parser(action)
    p = sub.add_parser("approve", help="Approuver une tâche locale")
    p.add_argument("task_id")
    p = sub.add_parser("prototype", help="Générer le prototype d'une tâche approuvée (n'exécute pas le code)")
    p.add_argument("task_id")
    p = sub.add_parser("measure", help="Consigner le résultat humain d'une expérimentation")
    p.add_argument("task_id")
    p.add_argument("--success", required=True, type=int, choices=(0, 1))
    p.add_argument("--value-eur", default=0.0, type=float)
    p.add_argument("--notes", default="")
    args = parser.parse_args()
    try:
        settings, sessions, budget = build()
        if args.action == "init":
            print(f"Base initialisée. Mode : {'SIMULATION' if settings.mock else 'API RÉELLES'}")
        elif args.action == "once":
            run_meeting(settings, sessions, budget)
        elif args.action == "status":
            print(f"Mode {'SIMULATION' if settings.mock else 'API RÉELLES'} ; budget mensuel local : {budget.totals():.5f}/{settings.monthly_limit} €")
            with sessions() as db:
                recent = db.scalars(select(Run).order_by(Run.started_at.desc()).limit(5)).all()
                tasks = db.scalars(select(Task).order_by(Task.created_at.desc()).limit(20)).all()
            for r in recent:
                print(f"Réunion {r.id} [{r.status}] : {r.decision or 'pas de décision'}")
            for t in tasks:
                print(f"Tâche {t.id} [{t.category}/{t.status}] : {t.title[:90]}")
        elif args.action == "ranking":
            for row in rank_providers(sessions):
                print(row)
        elif args.action == "approve":
            with sessions.begin() as db:
                t = db.get(Task, args.task_id)
                if not t or t.status != "pending_review":
                    raise ValueError("Tâche inconnue ou déjà traitée")
                t.status = "approved"
            print("Tâche approuvée. Aucun outil externe n'a été lancé.")
        elif args.action == "prototype":
            provider, files = generate_prototype(sessions, settings, budget, args.task_id)
            print(f"Prototype écrit avec {provider} (code NON exécuté) :")
            for p in files:
                print(f"- {p}")
        elif args.action == "measure":
            with sessions.begin() as db:
                t = db.get(Task, args.task_id)
                if not t or t.status not in ("approved", "awaiting_evaluation"):
                    raise ValueError("Tâche inconnue / non approuvée")
                t.status = "completed"
                t.completed_at = datetime.now(timezone.utc)
                t.success = args.success
                t.value_eur = args.value_eur
                t.notes = args.notes[:2000]
            print("Résultat enregistré : évaluation humaine / métrique externe, non jugement autonome du modèle.")
        elif args.action == "dashboard":
            import uvicorn
            from .dashboard import create_app
            print("Dashboard local : http://127.0.0.1:8000")
            uvicorn.run(create_app(sessions, settings, budget), host="127.0.0.1", port=8000)
        elif args.action == "scheduler":
            from apscheduler.schedulers.blocking import BlockingScheduler
            from apscheduler.triggers.cron import CronTrigger
            scheduler = BlockingScheduler(timezone=settings.timezone)
            scheduler.add_job(lambda: run_meeting(settings, sessions, budget),
                              CronTrigger(hour=settings.schedule_hour, minute=settings.schedule_minute,
                                          timezone=settings.timezone),
                              max_instances=1, coalesce=True, id="daily_board", replace_existing=True)
            print(f"Planificateur actif : {settings.schedule_hour:02d}:{settings.schedule_minute:02d}, {settings.timezone} (Ctrl+C pour arrêter)")
            scheduler.start()
    except (ValueError, RuntimeError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
