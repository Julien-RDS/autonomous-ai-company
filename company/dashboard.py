"""Tableau de bord local en lecture seule, à ne pas exposer sur internet."""
from __future__ import annotations

from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from .database import Run, Task, SandboxRun


def create_app(sessions, settings, budget) -> FastAPI:
    app = FastAPI(title="Autonomous AI Company — suivi local")

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
                 'profile':r.profile,'exit_code':r.exit_code,'duration_ms':r.duration_ms,
                 'started_at':r.started_at.isoformat(),'image_id':r.image_id} for r in rows]

    @app.get("/", response_class=HTMLResponse)
    def home():
        data = status()
        badge = "SIMULATION" if settings.mock else "API RÉELLES"
        recent = "".join(
            f"<tr><td><code>{escape(r['id'][:10])}</code></td><td>{escape(r['status'])}</td>"
            f"<td>{escape(r['decision'] or '—')}</td></tr>" for r in data["runs"]
        ) or "<tr><td colspan='3'>Aucune réunion à ce jour.</td></tr>"
        isolated = "".join(
            f"<tr><td>{escape(r['task_id'])}</td><td>{r['attempt']}</td>"
            f"<td>{escape(r['profile'])}</td><td>{escape(r['state'])}</td>"
            f"<td>{r['duration_ms'] if r['duration_ms'] is not None else '—'}</td></tr>"
            for r in sandbox_runs()
        ) or "<tr><td colspan='5'>Aucune exécution isolée.</td></tr>"
        tasks = "".join(
            f"<tr><td><code>{escape(t['id'][:10])}</code></td><td>{escape(t['category'])}</td>"
            f"<td>{escape(t['status'])}</td><td>{escape(t['title'][:150])}</td></tr>" for t in data["tasks"]
        ) or "<tr><td colspan='4'>Aucune tâche.</td></tr>"
        return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autonomous AI Company</title><style>
:root{{--bg:#0c111e;--panel:#141e30;--stroke:#2c3a52;--text:#f2f6ff;--muted:#a9b8cd;--accent:#7bd7c6}}
*{{box-sizing:border-box}}body{{font-family:Inter,system-ui,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0;line-height:1.5}}
main{{max-width:1160px;margin:auto;padding:38px 22px}}h1{{font-size:clamp(28px,4vw,43px);margin:0 0 7px}}
p{{color:var(--muted)}}.meta{{display:flex;gap:12px;align-items:center;flex-wrap:wrap}}.tag{{border:1px solid var(--accent);border-radius:99px;padding:4px 13px;color:var(--accent);font-size:12px;font-weight:700}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin:24px 0}}.card,section{{background:var(--panel);border:1px solid var(--stroke);border-radius:15px;padding:22px}}
.value{{font-size:33px;font-weight:700;color:var(--accent)}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{padding:12px 8px;border-bottom:1px solid var(--stroke);text-align:left;vertical-align:top}}td{{overflow-wrap:anywhere}}th{{color:var(--muted)}}section{{margin-top:16px;overflow-x:auto}}
a{{color:var(--accent)}}code{{font-size:12px}}
</style></head><body><main><div class="meta"><span class="tag">{escape(badge)}</span><span>Tableau de bord local · lecture seule</span></div>
<h1>Autonomous AI Company</h1><p>Débats intermodèles, décisions, coûts comptabilisés et tâches nécessitant une validation humaine.</p>
<div class="grid"><div class="card"><div>Budget mensuel configuré</div><div class="value">{escape(data['monthly_budget_eur'])} €</div></div>
<div class="card"><div>Usage comptabilisé</div><div class="value">{escape(data['accounted_eur'])} €</div></div>
<div class="card"><div>Réunions enregistrées</div><div class="value">{len(data['runs'])}</div></div>
<div class="card"><div>Tâches en suivi</div><div class="value">{len(data['tasks'])}</div></div></div>
<section><h2>Décisions récentes</h2><table><thead><tr><th>ID</th><th>État</th><th>Décision</th></tr></thead><tbody>{recent}</tbody></table></section>
<section><h2>Tâches</h2><table><thead><tr><th>ID</th><th>Type</th><th>État</th><th>Action</th></tr></thead><tbody>{tasks}</tbody></table></section>
<section><h2>Exécutions isolées</h2><table><thead><tr><th>Tâche</th><th>Tentative</th><th>Profil</th><th>État</th><th>Durée (ms)</th></tr></thead><tbody>{isolated}</tbody></table></section>
<p>API JSON locale : <a href="/api/status">/api/status</a>. Les recherches externes et les coûts opérateurs ne sont pas vérifiés depuis cette interface.</p>
</main></body></html>"""

    return app
