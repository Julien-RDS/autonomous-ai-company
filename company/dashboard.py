"""Tableau de bord local en lecture seule, à ne pas exposer sur internet."""
from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import select

from .database import Run, Task, SandboxRun
from .supervision import loads, register


def create_app(sessions, settings, budget) -> FastAPI:
    app = FastAPI(title="Autonomous AI Company — suivi local")
    # Reject DNS-rebinding Host headers even on read-only GET routes.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.get("/api/status")
    def status():
        with sessions() as db:
            recent = db.scalars(select(Run).order_by(Run.started_at.desc()).limit(20)).all()
            tasks = db.scalars(select(Task).order_by(Task.created_at.desc()).limit(50)).all()
        return {
            "mode": "simulation" if settings.mock else "API réelles",
            "monthly_budget_eur": str(settings.monthly_limit),
            "accounted_eur": str(round(budget.totals(), 6)),
            "runs": [{"id": r.id, "status": r.status, "mission": r.mission,
                      "decision": r.decision, "started_at": r.started_at.isoformat(),
                      "report_path": r.report_path} for r in recent],
            "tasks": [{"id": t.id, "title": t.title, "status": t.status,
                       "category": t.category, "provider": t.provider,
                       "success": t.success} for t in tasks],
        }

    @app.get('/api/sandbox-runs')
    def sandbox_runs():
        with sessions() as db:
            rows=db.scalars(select(SandboxRun).order_by(SandboxRun.started_at.desc()).limit(50)).all()
        return [{'id':r.id,'task_id':r.task_id,'attempt':r.attempt,'state':r.state,
                 'profile':r.profile,'validator_version':r.validator_version,
                 'manifest_sha256':r.manifest_sha256,'summary':loads(r.summary_json, None),
                 'exit_code':r.exit_code,'duration_ms':r.duration_ms,
                 'started_at':r.started_at.isoformat(),'image_id':r.image_id} for r in rows]

    # Read-only supervision pages (GET only): overview at "/", meetings, tasks,
    # queue, sandbox and budget. See company/supervision.py and docs/SUPERVISION.md.
    register(app, sessions, settings, budget)

    # Explicitly opt-in local web actions: no writes unless two secrets and permission allowlist exist.
    from .web_actions import register as register_actions
    register_actions(app, sessions, settings, budget)

    return app
