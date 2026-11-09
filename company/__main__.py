"""Commandes de démarrage et d'administration."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from .budget import Budget
from .config import Settings
from .database import Run, Task, Approval, AuditEvent, session_factory
from .prototyping import generate_prototype
from .routing import rank_providers
from .workflow import Board


def human_actor(explicit: str | None) -> str:
    """Identity recorded with an approval; never a secret, bounded length."""
    if explicit is None:
        import getpass
        try:
            explicit = f"cli:{getpass.getuser()}"
        except (KeyError, OSError):
            explicit = "local-cli"
    explicit = explicit.strip()
    if not explicit or len(explicit) > 100 or not explicit.isprintable():
        raise ValueError("--actor : identité lisible de 1 à 100 caractères requise")
    return explicit


def sandbox_command(args, sessions):
    from . import sandbox
    if args.sandbox_action == "approve":
        if not args.reviewed:
            raise ValueError("Relire le code puis confirmer avec --reviewed ; l'approbation de génération ne suffit pas")
        image_id = sandbox.allowed_images().get(args.profile)
        if image_id is None:
            raise ValueError("Aucune image autorisée : définir SANDBOX_PYTHON_IMAGE_ID (sha256 vérifié)")
        approval = sandbox.approve_execution(sessions, args.task_id, profile=args.profile, image_id=image_id,
                                             actor=human_actor(args.actor), reason=args.reason)
        from .database import ExecutionApproval
        with sessions() as db:
            record = db.get(ExecutionApproval, approval)
        print(f"Exécution autorisée une seule fois ({approval}). Rien n'a été lancé.")
        print(f"Contenu autorisé (SHA-256) : {record.manifest_sha256} ; profil {record.profile} ; image {record.image_id}")
    elif args.sandbox_action == "run":
        sandbox.cleanup(sessions, actor="cli:sandbox-run")  # expired leftovers only
        run = sandbox.run_approved(sessions, args.task_id)
        print(f"Tentative {run.attempt} : {run.state} (code {run.exit_code}, {run.duration_ms} ms, "
              f"{run.output_bytes} octets{', tronqués' if run.truncated else ''})")
        if run.detail:
            print(f"Détail : {sandbox.display_safe(run.detail)}")
        # Untrusted container output: control/escape characters are neutralized.
        print("--- stdout (non fiable, borné, caractères de contrôle neutralisés) ---")
        print(sandbox.display_safe(run.stdout))
        print("--- stderr (non fiable, borné, caractères de contrôle neutralisés) ---")
        print(sandbox.display_safe(run.stderr))
    elif args.sandbox_action == "status":
        from .database import SandboxRun
        with sessions() as db:
            runs = db.scalars(select(SandboxRun).where(SandboxRun.task_id == args.task_id)
                              .order_by(SandboxRun.attempt)).all()
        for run in runs:
            print(f"#{run.attempt} run={run.id} {run.state} code={run.exit_code} image={run.image_id[:19]} "
                  f"contenu={run.manifest_sha256[:12]} début={run.started_at:%Y-%m-%d %H:%M:%S}")
        if not runs:
            print("Aucune exécution isolée pour cette tâche.")
    elif args.sandbox_action == "probe":
        import json as _json
        image_id = args.image_id or sandbox.allowed_images().get("python-unittest")
        if not image_id:
            raise ValueError("--image-id ou SANDBOX_PYTHON_IMAGE_ID requis")
        report = sandbox.check_limits(image_id)
        caps = report["capabilities"]
        print(f"cgroup v{caps.get('cgroup_version')} ({caps.get('cgroup_driver')}) ; rootless : "
              f"{'oui' if caps.get('rootless') else 'non'} ; capacités manquantes : {', '.join(caps['missing']) or 'aucune'}")
        print("Valeurs observées dans le conteneur : " + sandbox.display_safe(_json.dumps(report["observed"])))
        print("Écarts : " + (", ".join(report["problems"]) or "aucun — limites appliquées"))
        if report["problems"]:
            raise RuntimeError("Limites non appliquées : exécutions refusées sur cet hôte")
    elif args.sandbox_action == "doctor":
        report = sandbox.diagnose(sessions)
        caps = report["capabilities"]
        print(f"Docker joignable : {'oui' if caps['reachable'] else 'NON'} ; cgroup v{caps.get('cgroup_version')} "
              f"({caps.get('cgroup_driver')}) ; rootless : {'oui' if caps.get('rootless') else 'non'}")
        print("Capacités manquantes : " + (", ".join(caps["missing"]) if caps["missing"]
              else "aucune déclarée (l'application réelle est vérifiée à chaque exécution)"))
        for run in report["running"]:
            print(f"Run {run['run_id']} tâche {run['task_id']} #{run['attempt']} : {run['status']}")
        if not report["running"]:
            print("Aucune exécution RUNNING.")
        for name in report["unknown_containers"]:
            print(f"Conteneur sandbox sans exécution connue : {name}")
    elif args.sandbox_action == "cleanup" and args.run:
        if args.all:
            raise ValueError("--run et --all sont exclusifs")
        if not args.reason:
            raise ValueError("--reason est obligatoire pour un nettoyage ciblé")
        report = sandbox.cleanup_run(sessions, args.run, actor=human_actor(args.actor), reason=args.reason)
        print(f"Run {report['run_id']} ({report['previous_state']}) : {report['outcome']} ; conteneurs supprimés : "
              f"{len(report['containers_removed'])} ; espace supprimé : {'oui' if report['workspace_removed'] else 'non'}")
        print("Aucune approbation consommée, aucun test relancé.")
    elif args.sandbox_action == "cleanup":
        report = sandbox.cleanup(sessions, force=args.all, actor=human_actor(args.actor),
                                 reason=args.reason or ("nettoyage forcé" if args.all else "nettoyage des orphelins échus"))
        print(f"Conteneurs supprimés : {len(report['containers_removed'])} ; "
              f"exécutions marquées interrompues : {len(report['runs_interrupted'])}")


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
    p.add_argument("--actor", default=None, help="Identité humaine consignée (défaut : utilisateur système)")
    p = sub.add_parser("prototype", help="Générer le prototype d'une tâche approuvée (n'exécute pas le code)")
    p.add_argument("task_id")
    p = sub.add_parser("reconcile-cost", help="Consigner un coût inconnu vérifié par un humain")
    p.add_argument("ledger_id")
    p.add_argument("--amount-eur", required=True, type=Decimal)
    p.add_argument("--reference", required=True)
    p = sub.add_parser("measure", help="Consigner le résultat humain d'une expérimentation")
    p.add_argument("task_id")
    p.add_argument("--success", required=True, type=int, choices=(0, 1))
    p.add_argument("--value-eur", default=0.0, type=Decimal)
    p.add_argument("--notes", default="")
    p = sub.add_parser("sandbox", help="Lot 3 : tests isolés dans Docker, après approbation d'exécution dédiée")
    sandbox = p.add_subparsers(dest="sandbox_action", required=True)
    sp = sandbox.add_parser("approve", help="Autoriser UNE exécution du contenu actuel d'un prototype généré")
    sp.add_argument("task_id")
    sp.add_argument("--profile", default="python-unittest")
    sp.add_argument("--actor", default=None)
    sp.add_argument("--reason", required=True, help="Motif humain (pas de secret)")
    sp.add_argument("--reviewed", action="store_true",
                    help="Confirme que le code a été relu avant d'autoriser son exécution isolée")
    sp = sandbox.add_parser("run", help="Consommer une autorisation et lancer les tests dans Docker")
    sp.add_argument("task_id")
    sp = sandbox.add_parser("status", help="Historique des exécutions d'une tâche")
    sp.add_argument("task_id")
    sp = sandbox.add_parser("cleanup", help="Supprimer conteneurs/espaces orphelins, marquer les exécutions interrompues")
    sp.add_argument("--all", action="store_true",
                    help="Tout supprimer, y compris avant échéance : seulement si aucune exécution n'est en cours")
    sp.add_argument("--run", default=None, help="Nettoyage ciblé d'une seule exécution (identifiant de run)")
    sp.add_argument("--actor", default=None)
    sp.add_argument("--reason", default=None, help="Motif consigné (obligatoire avec --run)")
    sandbox.add_parser("doctor", help="Diagnostic lecture seule : capacités Docker, exécutions RUNNING, orphelins")
    sp = sandbox.add_parser("probe", help="Lire les limites cgroup effectives avec la sonde fixe (aucun code de prototype)")
    sp.add_argument("--image-id", default=None, help="ID sha256 local (défaut : SANDBOX_PYTHON_IMAGE_ID)")
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
            from .approvals import approve_task
            if sessions.kw['bind'].dialect.name == 'postgresql':
                from .worker import checkpoints
                with checkpoints(settings.db_url, f"approval:{args.task_id}") as saver:
                    approve_task(sessions, args.task_id, actor=human_actor(args.actor), checkpointer=saver)
            else:
                approve_task(sessions, args.task_id, actor=human_actor(args.actor))
            print("Tâche approuvée. Aucun outil externe n'a été lancé.")
        elif args.action == "prototype":
            provider, files = generate_prototype(sessions, settings, budget, args.task_id)
            print(f"Prototype écrit avec {provider} (code NON exécuté) :")
            for p in files:
                print(f"- {p}")
        elif args.action == "reconcile-cost":
            budget.reconcile_unknown(args.ledger_id, args.amount_eur, args.reference)
            print("Coût rapproché. Aucune opération externe n'a été relancée.")
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
        elif args.action == "sandbox":
            sandbox_command(args, sessions)
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
