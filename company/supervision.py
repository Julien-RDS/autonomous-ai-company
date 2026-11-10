"""Supervision locale en lecture seule.

Principes :
- uniquement des routes GET ; aucune lecture ne valide une transaction (pas de commit),
  ne démarre un workflow, n'approuve, ne relance, n'exécute ni ne rapproche quoi que ce soit ;
- les interventions humaines sont indiquées sous forme de commandes CLI expliquées ;
- toute donnée issue d'un modèle, d'une recherche ou d'un conteneur est non fiable :
  elle est échappée, tronquée, et jamais interprétée (aucun script n'est autorisé par la CSP) ;
- aucun chemin n'est accepté depuis le navigateur : le rapport est recalculé à partir de
  l'identifiant validé de la réunion, et les fichiers d'un prototype sont lus dans le
  manifeste en base par numéro ;
- listes paginées, une requête par liste (pas de requête par élément).
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .database import (Approval, Attempt, AuditEvent, ExecutionApproval, Ledger, Message, Run, SandboxCleanup,
                       SandboxEvidence, SandboxRun, Task, WorkItem, WorkReview)

PAGE_SIZE = 25
MAX_PAGE = 10_000
REPORT_MAX_BYTES = 512 * 1024
TEXT_MAX_CHARS = 64 * 1024
ID = re.compile(r"^[0-9a-f]{32}$")

SECURITY_HEADERS = {
    # No script at all: the dashboard never interprets content it displays.
    "Content-Security-Policy": ("default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    # Browsers may send Origin: null on form POSTs under no-referrer, which
    # correctly fails the actions' origin check. Keep same-origin forms usable
    # without disclosing a referrer to other origins.
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

LEDGER_STATES = {
    "reserved": ("Réservation initiale", "Estimation prudente bloquée avant l'appel ; l'appel peut être en cours."),
    "settled": ("Coût calculé depuis les tokens", "Calcul local à partir des tokens et des tarifs configurés : "
                "ce n'est pas une facture de l'opérateur."),
    "unknown": ("Coût inconnu", "L'appel a pu être facturé sans résultat confirmé : la réservation est conservée "
                "jusqu'au rapprochement humain."),
    "reconciled": ("Rapproché par un humain", "Montant confirmé par un humain avec une référence externe."),
}
STAGE_ORDER = {"proposal": 0, "research": 1, "critique": 2, "vote": 3, "decision": 4}
WORK_STATES = ("PENDING", "RUNNING", "RETRY_SCHEDULED", "WAITING_APPROVAL", "FAILED", "SUCCEEDED", "CANCELLED")
TONE = {
    "PASSED": "ok", "SUCCEEDED": "ok", "completed": "ok", "settled": "ok", "reconciled": "ok",
    "FAILED": "bad", "ERROR": "bad", "TIMEOUT": "bad", "OOM": "bad", "OUTPUT_LIMIT": "bad", "failed": "bad",
    "unknown": "warn", "WAITING_APPROVAL": "warn", "pending_review": "warn", "INTERRUPTED": "warn",
    "RETRY_SCHEDULED": "warn", "awaiting_evaluation": "warn",
    "RUNNING": "info", "running": "info", "PENDING": "info", "reserved": "info", "approved": "info",
}

CSS = """
:root{--bg:#f6f7f9;--panel:#fff;--stroke:#d9dee6;--text:#16202e;--muted:#536176;--accent:#0b6e5f;
--ok:#126b3a;--okbg:#e3f4ea;--bad:#a1231a;--badbg:#fbe5e3;--warn:#7a4b00;--warnbg:#fdf0d5;--info:#14467a;--infobg:#e3edf9;
--code:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#0d131d;--panel:#151e2b;--stroke:#2b384a;--text:#eef3fa;--muted:#a8b6c9;
--accent:#6fd3c1;--ok:#8be0a9;--okbg:#173525;--bad:#ffb1a8;--badbg:#45201c;--warn:#ffd88a;--warnbg:#3f3016;
--info:#a9cdff;--infobg:#1b3150;--code:#1d2838}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
background:var(--bg);color:var(--text);line-height:1.5}
a{color:var(--accent)}a:focus,summary:focus{outline:3px solid var(--accent);outline-offset:2px}
header{background:var(--panel);border-bottom:1px solid var(--stroke)}
.bar{max-width:1200px;margin:auto;padding:12px 20px;display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}
.brand{font-weight:700;margin-right:auto}nav{display:flex;flex-wrap:wrap;gap:4px}
nav a{padding:6px 10px;border-radius:8px;text-decoration:none;color:var(--text)}
nav a[aria-current=page]{background:var(--code);font-weight:600}
main{max-width:1200px;margin:auto;padding:22px 20px 60px}h1{font-size:1.6rem;margin:4px 0 6px}
h2{font-size:1.15rem;margin:0 0 10px}.muted{color:var(--muted)}
.mode{display:inline-block;padding:4px 12px;border-radius:99px;font-weight:700;font-size:.85rem;letter-spacing:.03em}
.mode.mock{background:var(--infobg);color:var(--info)}.mode.real{background:var(--badbg);color:var(--bad)}
.grid{display:flex;flex-wrap:wrap;gap:14px;margin:16px 0}.grid>.card{flex:1 1 210px;min-width:0}
.card,section{background:var(--panel);border:1px solid var(--stroke);border-radius:12px;padding:16px}
section{margin-top:16px;overflow-x:auto}.big{font-size:1.6rem;font-weight:700}
table{border-collapse:collapse;width:100%;font-size:.92rem}th,td{padding:8px 8px;border-bottom:1px solid var(--stroke);
text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}td{overflow-wrap:anywhere}
.tag{display:inline-block;padding:1px 8px;border-radius:6px;font-size:.8rem;font-weight:600;background:var(--code)}
.tag.ok{background:var(--okbg);color:var(--ok)}.tag.bad{background:var(--badbg);color:var(--bad)}
.tag.warn{background:var(--warnbg);color:var(--warn)}.tag.info{background:var(--infobg);color:var(--info)}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85rem}
code{background:var(--code);padding:1px 5px;border-radius:5px}
pre{background:var(--code);padding:12px;border-radius:8px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;max-height:70vh}
.cmd{border-left:4px solid var(--accent);background:var(--code);padding:10px 12px;border-radius:6px;margin:8px 0}
.cmd p{margin:4px 0 0;color:var(--muted);font-size:.88rem}
.notice{border-left:4px solid var(--warn);background:var(--warnbg);padding:10px 12px;border-radius:6px;margin:12px 0}
.pager{display:flex;gap:12px;align-items:center;margin-top:10px}dl{display:grid;grid-template-columns:max-content 1fr;
gap:4px 16px;margin:0}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}
.filters{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.filters a{padding:3px 9px;border:1px solid var(--stroke);border-radius:99px;text-decoration:none;font-size:.85rem}
.filters a[aria-current=true]{background:var(--code);font-weight:600}
"""

NAV = (("/", "Vue d'ensemble"), ("/runs", "Réunions"), ("/tasks", "Tâches"), ("/work", "File"),
       ("/sandbox", "Sandbox"), ("/budget", "Budget"), ("/actions", "Actions"))


# ------------------------------------------------------------------ helpers

def h(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def visible(text: str) -> str:
    """Untrusted text for <pre>: control and bidi characters become readable escapes, then HTML-escaped."""
    return h("".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C")
                     else (f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}") for c in text))


def clip(value, limit=300) -> str:
    text = "" if value is None else str(value)
    return h(text if len(text) <= limit else text[:limit] + "…")


def ts(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def tag(state) -> str:
    return f'<span class="tag {TONE.get(str(state), "")}">{h(state)}</span>'


def short(identifier: str | None) -> str:
    return h((identifier or "")[:10])


def eur(value: Decimal | None) -> str:
    return "—" if value is None else f"{Decimal(value):.6f} €"


def command(cmd: str, effect: str) -> str:
    return f'<div class="cmd"><code>{h(cmd)}</code><p>{h(effect)}</p></div>'


def table(headers: list[str], rows: list[str], empty: str) -> str:
    head = "".join(f"<th scope='col'>{h(x)}</th>" for x in headers)
    body = "".join(rows) or f"<tr><td colspan='{len(headers)}' class='muted'>{h(empty)}</td></tr>"
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def pager(path: str, page: int, total: int, extra: dict | None = None) -> str:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    params = "".join(f"&{h(k)}={h(v)}" for k, v in (extra or {}).items() if v)
    prev = f'<a href="{path}?page={page - 1}{params}">← Précédent</a>' if page > 1 else ""
    nxt = f'<a href="{path}?page={page + 1}{params}">Suivant →</a>' if page < pages else ""
    return f'<div class="pager">{prev}<span class="muted">Page {page} / {pages} · {total} élément(s)</span>{nxt}</div>'


def offset(page: int) -> int:
    return (page - 1) * PAGE_SIZE


def valid_id(identifier: str) -> str:
    if not ID.match(identifier or ""):
        raise HTTPException(status_code=404)
    return identifier


def loads(raw: str | None, default):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def items(value, limit: int) -> list:
    """Bounded display of a JSON array; imported or damaged fields may have any type."""
    return value[:limit] if isinstance(value, list) else []


def summary_text(raw: str | None) -> str:
    data = loads(raw, None)
    if not isinstance(data, dict):
        return "Indisponible"
    if data.get("kind") == "unit" and isinstance(data.get("counts"), dict):
        c = data["counts"]
        return (f"découverts {c.get('discovered')}, réussis {c.get('passed')}, échecs {c.get('failed')}, "
                f"erreurs {c.get('errors')}, ignorés {c.get('skipped')}")
    if data.get("kind") == "static":
        return "structure HTML déclarée valide"
    return "Format inconnu"


def profile_label(profile: str) -> str:
    return {"python-unittest": "Tests fonctionnels Python",
            "static-html": "Validation statique HTML (aucun JavaScript exécuté)"}.get(profile, profile)


# ------------------------------------------------------------------ next human actions

def task_action(task: Task, approved: bool, pending_exec: bool, has_manifest: bool) -> str:
    tid = task.id
    if task.status == "pending_review" and not approved:
        effect = ("Autorise uniquement la génération de fichiers ; n'autorise aucune exécution."
                  if task.category == "prototype" else
                  "Consigne votre accord pour mener l'expérimentation réelle ; rien n'est lancé automatiquement.")
        return command(f"python -m company approve {tid} --actor VOTRE_NOM", effect)
    if task.category == "prototype" and not has_manifest and task.status == "approved":
        return command(f"python -m company prototype {tid}",
                       "Génère les fichiers du prototype (MOCK par défaut) ; n'exécute aucun code.")
    if task.category == "prototype" and has_manifest and task.status == "awaiting_evaluation":
        if pending_exec:
            return command(f"python -m company sandbox run {tid}",
                           "Consomme UNE autorisation d'exécution et lance les tests dans Docker isolé.")
        manifest = loads(task.artifact_manifest, {})
        files = items(manifest.get("files"), 50) if isinstance(manifest, dict) else []
        extensions = {Path(f["path"]).suffix.lower() for f in files
                      if isinstance(f, dict) and isinstance(f.get("path"), str)}
        profile = "static-html" if ".html" in extensions and ".py" not in extensions else "python-unittest"
        return command(f'python -m company sandbox approve {tid} --reviewed --profile {profile} '
                       '--actor VOTRE_NOM --reason "code relu"',
                       f"Après relecture de artifacts/prototypes/{tid}/ : autorise UNE exécution isolée de ce "
                       "contenu exact. Configurer SANDBOX_PYTHON_IMAGE_ID avec une image locale vérifiée. "
                       "L'approbation de génération ne suffit pas.")
    if task.status in ("approved", "awaiting_evaluation"):
        return command(f'python -m company measure {tid} --success 0 --notes "observation réelle"',
                       "Consigne le résultat humain : 0 pour échec, remplacer par 1 pour succès. "
                       "Renseigner les observations réelles avant de lancer la commande.")
    return "<span class='muted'>Aucune action requise.</span>"


def work_action(item: WorkItem) -> str:
    if item.state in ("FAILED", "WAITING_APPROVAL"):
        if item.external:
            return ("<span class='muted'>Effet externe possible : intervention manuelle, jamais rejoué "
                    "automatiquement.</span>")
        return command(f'python -m company.worker requeue {item.id} --actor VOTRE_NOM --reason "motif"',
                       "Remet ce travail MOCK en file après revue humaine ; l'historique est conservé.")
    if item.state in ("PENDING", "RETRY_SCHEDULED"):
        return command("python -m company.worker run --once", "Traite un travail disponible (MOCK uniquement).")
    return "<span class='muted'>Aucune action requise.</span>"


# ------------------------------------------------------------------ layout

def page(settings, title: str, body: str, active: str, generated: datetime) -> HTMLResponse:
    mode = ('<span class="mode mock">SIMULATION (MOCK)</span>' if settings.mock
            else '<span class="mode real">API RÉELLES — coûts possibles</span>')
    links = "".join(f'<a href="{href}"{" aria-current=page" if href == active else ""}>{h(label)}</a>'
                    for href, label in NAV)
    html = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{h(title)} · Autonomous AI Company</title>
<link rel="stylesheet" href="/assets/supervision.css"></head><body>
<header><div class="bar"><span class="brand">Autonomous AI Company</span>{mode}
<nav aria-label="Navigation principale">{links}</nav></div></header>
<main><p class="muted">Supervision locale en lecture seule · données au {h(ts(generated))} ·
<a href="">Actualiser</a></p>{body}</main></body></html>"""
    return HTMLResponse(html)


def not_found(settings, what: str) -> HTMLResponse:
    response = page(settings, "Introuvable", f"<h1>Introuvable</h1><p>{h(what)}</p><p><a href='/'>Retour</a></p>",
                    "", datetime.now(timezone.utc))
    response.status_code = 404
    return response


# ------------------------------------------------------------------ routes

def register(app: FastAPI, sessions, settings, budget) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, exc: SQLAlchemyError):
        # No trace, DSN or SQL: only a neutral message.
        response = page(settings, "Base indisponible",
                        "<h1>Base de données indisponible</h1><p>Les données n'ont pas pu être lues. "
                        "Aucune action n'a été effectuée. Réessayez plus tard.</p>", "", datetime.now(timezone.utc))
        response.status_code = 503
        return response

    @app.exception_handler(StarletteHTTPException)  # also unmatched routes, not only raised 404s
    async def http_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return not_found(settings, "Cet élément n'existe pas ou l'identifiant est invalide.")
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code)

    @app.get("/assets/supervision.css", include_in_schema=False)
    def stylesheet():
        return Response(CSS, media_type="text/css")

    # ---------------------------------------------------------- overview
    @app.get("/", response_class=HTMLResponse)
    def overview():
        now = datetime.now(timezone.utc)
        with sessions() as db:
            runs_by = dict(db.execute(select(Run.status, func.count()).group_by(Run.status)).all())
            work_by = dict(db.execute(select(WorkItem.state, func.count()).group_by(WorkItem.state)).all())
            tasks_by = dict(db.execute(select(Task.status, func.count()).group_by(Task.status)).all())
            ledger = {state: (count, total) for state, count, total in db.execute(
                select(Ledger.state, func.count(), func.coalesce(func.sum(Ledger.amount_eur), 0))
                .group_by(Ledger.state)).all()}
            recent_runs = db.scalars(select(Run).order_by(Run.started_at.desc()).limit(5)).all()
            recent_sandbox = db.scalars(select(SandboxRun).order_by(SandboxRun.started_at.desc()).limit(5)).all()
            review = db.scalars(select(Task).where(Task.status == "pending_review")
                                .order_by(Task.created_at.desc()).limit(5)).all()
            generated = db.scalars(select(Task).where(Task.status == "awaiting_evaluation")
                                   .order_by(Task.created_at.desc()).limit(5)).all()
            stuck = db.scalars(select(WorkItem).where(WorkItem.state.in_(("FAILED", "WAITING_APPROVAL")))
                               .order_by(WorkItem.scheduled_at.desc()).limit(5)).all()
            unknown = db.scalars(select(Ledger).where(Ledger.state == "unknown")
                                 .order_by(Ledger.created_at.desc()).limit(5)).all()
            ids = [t.id for t in review + generated]
            approved = set(db.scalars(select(Approval.task_id).where(Approval.task_id.in_(ids))).all()) if ids else set()
            pending_exec = set(db.scalars(select(ExecutionApproval.task_id).where(
                ExecutionApproval.task_id.in_(ids), ExecutionApproval.consumed_at.is_(None))).all()) if ids else set()
        accounted = sum((Decimal(total) for _, total in ledger.values()), Decimal(0))
        uncertain = Decimal(ledger.get("unknown", (0, 0))[1])
        attention = []
        for t in review + generated:
            attention.append(f"<tr><td><a href='/tasks/{h(t.id)}'>{clip(t.title, 90)}</a><br>{tag(t.status)} "
                             f"<span class='muted'>{h(t.category)}</span></td>"
                             f"<td>{task_action(t, t.id in approved, t.id in pending_exec, bool(t.artifact_manifest))}</td></tr>")
        for w in stuck:
            attention.append(f"<tr><td><a href='/work/{h(w.id)}'>Travail {short(w.id)}</a><br>{tag(w.state)} "
                             f"<span class='muted'>{h(w.kind)}</span></td><td>{work_action(w)}</td></tr>")
        for item in unknown:
            attention.append(f"<tr><td><a href='/budget#l-{h(item.id)}'>Coût inconnu {short(item.id)}</a><br>"
                             f"{eur(item.amount_eur)} · {h(item.provider)}</td><td>"
                             + command(f'python -m company reconcile-cost {item.id} --amount-eur MONTANT '
                                       f'--reference "relevé opérateur"',
                                       "Consigne le montant vérifié sur le relevé de l'opérateur ; ne rejoue jamais "
                                       "l'appel.") + "</td></tr>")
        work_cells = "".join(f"<div class='card'><div class='muted'>{h(s)}</div><div class='big'>{work_by.get(s, 0)}</div>"
                             f"</div>" for s in ("PENDING", "RUNNING", "WAITING_APPROVAL", "FAILED"))
        runs_rows = [f"<tr><td><a href='/runs/{h(r.id)}'>{short(r.id)}</a></td><td>{tag(r.status)}</td>"
                     f"<td>{clip(r.decision or '—', 90)}</td><td>{h(ts(r.started_at))}</td></tr>" for r in recent_runs]
        sandbox_rows = [f"<tr><td><a href='/sandbox/{h(s.id)}'>{short(s.id)}</a></td>"
                        f"<td><a href='/tasks/{h(s.task_id)}'>{short(s.task_id)}</a></td><td>{s.attempt}</td>"
                        f"<td>{h(profile_label(s.profile))}</td><td>{tag(s.state)}</td>"
                        f"<td>{s.duration_ms if s.duration_ms is not None else '—'}</td>"
                        f"<td>{h(summary_text(s.summary_json))}</td></tr>" for s in recent_sandbox]
        body = f"""<h1>Vue d'ensemble</h1>
<p class="muted">Ce que fait l'entreprise simulée, ce qui attend votre intervention et ce qui a échoué.
Les simulations ne prouvent aucune demande commerciale.</p>
<div class="grid">{work_cells}
<div class="card"><div class="muted">Réunions (terminées / en cours / échouées)</div><div class="big">
{runs_by.get('completed', 0)} / {runs_by.get('running', 0)} / {runs_by.get('failed', 0)}</div></div>
<div class="card"><div class="muted">Tâches à valider</div><div class="big">{tasks_by.get('pending_review', 0)}</div></div></div>
<section aria-labelledby="att"><h2 id="att">À votre attention</h2>
<p class="muted">Aucun bouton : chaque intervention passe par la commande indiquée, avec ses propres contrôles.</p>
{table(["Élément", "Action humaine et effet"], attention, "Rien n'attend votre intervention.")}</section>
<section aria-labelledby="bud"><h2 id="bud">Budget</h2><div class="grid">
<div class="card"><div class="muted">Plafond mensuel configuré</div><div class="big">{h(settings.monthly_limit)} €</div></div>
<div class="card"><div class="muted">Comptabilisé (toutes écritures)</div><div class="big">{eur(accounted)}</div></div>
<div class="card"><div class="muted">Dont coûts inconnus</div><div class="big">{eur(uncertain)}</div>
<div class="muted">{ledger.get('unknown', (0, 0))[0]} écriture(s) à rapprocher</div></div></div>
<p><a href="/budget">Détail des réservations, coûts calculés, inconnus et rapprochés</a></p></section>
<section aria-labelledby="mtg"><h2 id="mtg">Réunions récentes</h2>
{table(["Réunion", "État", "Décision", "Début"], runs_rows, "Aucune réunion à ce jour.")}</section>
<section aria-labelledby="sbx"><h2 id="sbx">Exécutions isolées</h2>
<p class="muted">Résumé déclaré par le conteneur, non fiable : un résultat technique n'est ni une validation
commerciale ni une mesure globale de qualité.</p>
{table(["Exécution", "Tâche", "Tentative", "Type", "Verdict", "Durée (ms)", "Résumé déclaré (non fiable)"],
       sandbox_rows, "Aucune exécution isolée.")}</section>"""
        return page(settings, "Vue d'ensemble", body, "/", now)

    # ---------------------------------------------------------- runs
    @app.get("/runs", response_class=HTMLResponse)
    def runs(page_number: int = Query(1, alias="page", ge=1, le=MAX_PAGE)):
        now = datetime.now(timezone.utc)
        with sessions() as db:
            total = db.scalar(select(func.count()).select_from(Run))
            rows = db.scalars(select(Run).order_by(Run.started_at.desc())
                              .offset(offset(page_number)).limit(PAGE_SIZE)).all()
            ids = [r.id for r in rows]
            task_count = dict(db.execute(select(Task.run_id, func.count()).where(Task.run_id.in_(ids))
                                         .group_by(Task.run_id)).all()) if ids else {}
            cost = dict(db.execute(select(Ledger.run_id, func.sum(Ledger.amount_eur)).where(Ledger.run_id.in_(ids))
                                   .group_by(Ledger.run_id)).all()) if ids else {}
        lines = [f"<tr><td><a href='/runs/{h(r.id)}'>{short(r.id)}</a></td><td>{tag(r.status)}</td>"
                 f"<td>{h(r.mode)}</td><td>{clip(r.decision or '—', 100)}</td><td>{task_count.get(r.id, 0)}</td>"
                 f"<td>{eur(cost.get(r.id))}</td><td>{h(ts(r.started_at))}</td></tr>" for r in rows]
        body = ("<h1>Réunions et décisions</h1>"
                + table(["Réunion", "État", "Mode", "Décision", "Tâches", "Coût comptabilisé", "Début"], lines,
                        "Aucune réunion sur cette page.") + pager("/runs", page_number, total))
        return page(settings, "Réunions", f"<section>{body}</section>", "/runs", now)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(run_id: str):
        now = datetime.now(timezone.utc)
        valid_id(run_id)
        with sessions() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404)
            messages = db.scalars(select(Message).where(Message.run_id == run_id)
                                  .order_by(Message.id).limit(200)).all()
            tasks = db.scalars(select(Task).where(Task.run_id == run_id).order_by(Task.created_at)).all()
            cost = db.execute(select(Ledger.state, func.count(), func.sum(Ledger.amount_eur))
                              .where(Ledger.run_id == run_id).group_by(Ledger.state)).all()
        result = loads(run.result_json, {})
        result = result if isinstance(result, dict) else {}
        proposals = [p for p in items(result.get("proposals"), 20) if isinstance(p, dict)]
        critiques = result.get("critiques") if isinstance(result.get("critiques"), dict) else {}
        votes = [v for v in items(result.get("votes"), 20) if isinstance(v, dict)]
        decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
        sources = result.get("sources") if isinstance(result.get("sources"), dict) else {}
        errors = [str(e) for e in items(result.get("errors"), 20)]
        prop_rows = [f"<tr><td>{h(p.get('id'))}</td><td><strong>{clip(p.get('title'), 160)}</strong><br>"
                     f"{clip(p.get('problem'), 400)}</td><td>{clip(p.get('customer'), 200)}</td>"
                     f"<td>{clip(p.get('experiment'), 300)}<br><span class='muted'>Critère : "
                     f"{clip(p.get('success_metric'), 200)}</span></td></tr>" for p in proposals]
        crit_rows = [f"<tr><td>{h(agent)}</td><td>{clip(text, 1500)}</td></tr>"
                     for agent, text in list(critiques.items())[:20]]
        vote_rows = [f"<tr><td>{h(v.get('agent'))}</td><td>{h(v.get('proposal_id'))}</td>"
                     f"<td>{clip(v.get('reason'), 500)}</td><td>{clip(v.get('next_action'), 300)}</td></tr>" for v in votes]
        source_rows = []
        for proposal_id, source_items in list(sources.items())[:20]:
            for s in items(source_items, 5):
                if isinstance(s, dict):
                    # URLs are shown as text, never as clickable links: they are unverified.
                    source_rows.append(f"<tr><td>{h(proposal_id)}</td><td>{clip(s.get('title'), 160)}</td>"
                                       f"<td><code>{clip(s.get('url'), 300)}</code></td>"
                                       f"<td>{clip(s.get('snippet'), 600)}</td></tr>")
        chosen = decision.get("proposal") if isinstance(decision.get("proposal"), dict) else {}
        # Messages carry no timestamp: order by cycle stage, never by random identifier.
        messages = sorted(messages, key=lambda m: (STAGE_ORDER.get(m.stage, 9), m.provider))
        timeline = [f"<tr><td>{h(m.stage)}</td><td>{h(m.provider)}</td><td>{h(m.model)}</td>"
                    f"<td>{m.input_tokens} / {m.output_tokens}</td><td>{m.latency_ms}</td>"
                    f"<td>{tag('ok' if m.success else 'échec')}</td></tr>" for m in messages]
        task_rows = [f"<tr><td><a href='/tasks/{h(t.id)}'>{short(t.id)}</a></td><td>{h(t.category)}</td>"
                     f"<td>{tag(t.status)}</td><td>{clip(t.title, 150)}</td></tr>" for t in tasks]
        cost_rows = [f"<tr><td>{h(LEDGER_STATES.get(s, (s,))[0])}</td><td>{n}</td><td>{eur(total)}</td></tr>"
                     for s, n, total in cost]
        report = (f"<a href='/runs/{h(run.id)}/report'>Lire le rapport</a> · "
                  f"<a href='/runs/{h(run.id)}/report?download=1'>Télécharger (texte)</a>"
                  if run.report_path else "<span class='muted'>Aucun rapport enregistré.</span>")
        body = f"""<h1>Réunion {short(run.id)}</h1>
<section><dl><dt>Mission</dt><dd>{clip(run.mission, 2000)}</dd><dt>État</dt><dd>{tag(run.status)}</dd>
<dt>Mode</dt><dd>{h(run.mode)}</dd><dt>Début</dt><dd>{h(ts(run.started_at))}</dd>
<dt>Fin</dt><dd>{h(ts(run.finished_at))}</dd><dt>Rapport</dt><dd>{report}</dd></dl></section>
<section><h2>Décision</h2><dl><dt>Proposition retenue</dt><dd>{clip(chosen.get('title') or run.decision or '—', 300)}</dd>
<dt>Méthode</dt><dd>{h(decision.get('selection_method', '—'))}</dd>
<dt>Votes</dt><dd>{h(decision.get('votes', '—'))} / {h(decision.get('total_votes', '—'))}
{'(égalité)' if decision.get('tie') else ''}</dd>
<dt>Prochaine action</dt><dd>{clip(decision.get('next_action'), 500)}</dd>
<dt>Marché vérifié</dt><dd>{'oui (déclaré)' if decision.get('verified_market') else 'non : hypothèse à tester'}</dd></dl>
<div class="notice">Une décision issue d'un débat entre modèles n'est pas une preuve de demande commerciale.</div></section>
<section><h2>Propositions</h2>{table(["Agent", "Proposition", "Client visé", "Expérience"], prop_rows, "Aucune proposition.")}</section>
<section><h2>Critiques</h2>{table(["Agent", "Critique"], crit_rows, "Aucune critique.")}</section>
<section><h2>Votes</h2>{table(["Votant", "Pour", "Justification", "Prochaine action"], vote_rows, "Aucun vote.")}</section>
<section><h2>Sources (non vérifiées)</h2><p class="muted">Extraits de recherche affichés comme texte ;
liens non cliquables, contenu non vérifié.</p>
{table(["Proposition", "Titre", "URL (texte)", "Extrait"], source_rows, "Aucune source disponible.")}</section>
<section><h2>Erreurs consignées</h2>{table(["Message"], [f"<tr><td>{clip(e, 300)}</td></tr>" for e in errors], "Aucune erreur.")}</section>
<section><h2>Réponses par étape du cycle</h2><p class="muted">Ordre du cycle proposition → recherche →
critique → vote ; l'heure de chaque réponse n'est pas enregistrée.</p>{table(["Étape", "Fournisseur", "Modèle", "Tokens entrée / sortie", "Latence (ms)", "Statut"], timeline, "Aucune réponse enregistrée.")}</section>
<section><h2>Tâches créées</h2>{table(["Tâche", "Type", "État", "Intitulé"], task_rows, "Aucune tâche.")}</section>
<section><h2>Coûts de la réunion</h2>{table(["Catégorie", "Écritures", "Montant"], cost_rows, "Aucune écriture.")}</section>"""
        return page(settings, f"Réunion {run.id[:10]}", body, "/runs", now)

    @app.get("/runs/{run_id}/report")
    def run_report(run_id: str, download: int = Query(0, ge=0, le=1)):
        now = datetime.now(timezone.utc)
        valid_id(run_id)
        with sessions() as db:
            run = db.get(Run, run_id)
            if run is None or not run.report_path:
                raise HTTPException(status_code=404)
        # The stored path is never trusted: the location is recomputed from the validated id.
        base = (config.ROOT / "reports").resolve()
        target = base / f"{run_id}.md"
        try:
            if target.is_symlink() or not target.is_file() or target.resolve().parent != base:
                raise HTTPException(status_code=404)
            with target.open("rb") as handle:
                raw = handle.read(REPORT_MAX_BYTES + 1)
        except OSError:
            raise HTTPException(status_code=404) from None
        truncated = len(raw) > REPORT_MAX_BYTES
        text = raw[:REPORT_MAX_BYTES].decode("utf-8", errors="replace")
        if download:
            return PlainTextResponse(text, headers={
                "Content-Disposition": f'attachment; filename="rapport-{run_id}.md.txt"'})
        body = (f"<h1>Rapport de la réunion {short(run_id)}</h1><p><a href='/runs/{h(run_id)}'>← Réunion</a> · "
                f"<a href='?download=1'>Télécharger</a></p><p class='muted'>Texte brut échappé, non interprété"
                f"{' — tronqué' if truncated else ''}.</p><pre>{visible(text)}</pre>")
        return page(settings, "Rapport", body, "/runs", now)

    # ---------------------------------------------------------- tasks
    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(page_number: int = Query(1, alias="page", ge=1, le=MAX_PAGE), status: str | None = Query(None)):
        now = datetime.now(timezone.utc)
        with sessions() as db:
            statuses = [s for s, in db.execute(select(Task.status).distinct().order_by(Task.status)).all()]
            if status not in statuses:
                status = None
            query = select(Task) if status is None else select(Task).where(Task.status == status)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            rows = db.scalars(query.order_by(Task.created_at.desc()).offset(offset(page_number)).limit(PAGE_SIZE)).all()
            ids = [t.id for t in rows]
            approved = set(db.scalars(select(Approval.task_id).where(Approval.task_id.in_(ids))).all()) if ids else set()
            pending_exec = set(db.scalars(select(ExecutionApproval.task_id).where(
                ExecutionApproval.task_id.in_(ids), ExecutionApproval.consumed_at.is_(None))).all()) if ids else set()
            work = dict(db.execute(select(WorkItem.task_id, WorkItem.state).where(WorkItem.task_id.in_(ids))).all()) \
                if ids else {}
        filters = "".join(f'<a href="/tasks?status={h(s)}"{" aria-current=true" if s == status else ""}>{h(s)}</a>'
                          for s in statuses)
        lines = [f"<tr><td><a href='/tasks/{h(t.id)}'>{short(t.id)}</a></td><td>{h(t.category)}</td>"
                 f"<td>{tag(t.status)}</td><td>{clip(t.title, 140)}</td>"
                 f"<td>{tag(work[t.id]) if t.id in work else '—'}</td>"
                 f"<td>{task_action(t, t.id in approved, t.id in pending_exec, bool(t.artifact_manifest))}</td></tr>"
                 for t in rows]
        body = ("<h1>Tâches (tickets métier)</h1><p class='muted'>Un ticket décrit une validation humaine ou un "
                "prototype ; le travail exécutable correspondant est dans la <a href='/work'>file</a>.</p>"
                f"<div class='filters'><a href='/tasks'{' aria-current=true' if status is None else ''}>Toutes</a>"
                f"{filters}</div>"
                + table(["Tâche", "Type", "État", "Intitulé", "Travail en file", "Prochaine action humaine"], lines,
                        "Aucune tâche sur cette page.") + pager("/tasks", page_number, total, {"status": status}))
        return page(settings, "Tâches", f"<section>{body}</section>", "/tasks", now)

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(task_id: str):
        now = datetime.now(timezone.utc)
        valid_id(task_id)
        with sessions() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404)
            approval = db.scalar(select(Approval).where(Approval.task_id == task_id))
            executions = db.scalars(select(ExecutionApproval).where(ExecutionApproval.task_id == task_id)
                                    .order_by(ExecutionApproval.created_at)).all()
            sandbox_runs = db.scalars(select(SandboxRun).where(SandboxRun.task_id == task_id)
                                      .order_by(SandboxRun.attempt)).all()
            work = db.scalar(select(WorkItem).where(WorkItem.task_id == task_id))
        manifest = loads(task.artifact_manifest, {})
        files = manifest.get("files") if isinstance(manifest, dict) and isinstance(manifest.get("files"), list) else []
        pending_exec = any(e.consumed_at is None for e in executions)
        file_rows = [f"<tr><td>{n}</td><td><code>{clip(f.get('path'), 200)}</code></td>"
                     f"<td>{len(str(f.get('content', '')).encode('utf-8'))} o</td>"
                     f"<td><a href='/tasks/{h(task_id)}/files/{n}'>Lire (texte échappé)</a> · "
                     f"<a href='/tasks/{h(task_id)}/files/{n}?download=1'>Télécharger</a></td></tr>"
                     for n, f in enumerate(files[:50]) if isinstance(f, dict)]
        exec_rows = [f"<tr><td>{short(e.id)}</td><td>{h(profile_label(e.profile))}</td><td><code>{clip(e.image_id, 80)}</code></td>"
                     f"<td><code>{h(e.manifest_sha256[:16])}</code></td><td>{h(e.actor)}</td><td>{clip(e.reason, 200)}</td>"
                     f"<td>{h(ts(e.created_at))}</td><td>{tag('consommée') if e.consumed_at else tag('disponible')}</td></tr>"
                     for e in executions]
        run_rows = [f"<tr><td><a href='/sandbox/{h(s.id)}'>#{s.attempt}</a></td><td>{h(profile_label(s.profile))}</td>"
                    f"<td>{tag(s.state)}</td><td>{h(summary_text(s.summary_json))}</td><td>{h(ts(s.started_at))}</td></tr>"
                    for s in sandbox_runs]
        gen = (f"{tag('accordée')} par {h(approval.actor)} le {h(ts(approval.created_at))}" if approval
               else tag("absente"))
        body = f"""<h1>{clip(task.title, 200)}</h1>
<section><dl><dt>Identifiant</dt><dd><code>{h(task.id)}</code></dd><dt>Type</dt><dd>{h(task.category)}</dd>
<dt>État</dt><dd>{tag(task.status)}</dd><dt>Réunion</dt><dd><a href='/runs/{h(task.run_id)}'>{short(task.run_id)}</a></dd>
<dt>Créée</dt><dd>{h(ts(task.created_at))}</dd><dt>Description</dt><dd>{clip(task.description, 2000)}</dd>
<dt>Résultat humain</dt><dd>{'—' if task.success is None else ('succès déclaré' if task.success else 'échec déclaré')}
{(' · ' + clip(task.notes, 400)) if task.notes else ''}</dd>
<dt>Travail en file</dt><dd>{f"<a href='/work/{h(work.id)}'>{short(work.id)}</a> {tag(work.state)}" if work else '—'}</dd></dl></section>
<section><h2>Prochaine action humaine</h2>{task_action(task, approval is not None, pending_exec, bool(files))}</section>
<section><h2>Approbations (séparées)</h2><dl><dt>Génération</dt><dd>{gen}</dd></dl>
<p class="muted">L'approbation de génération n'autorise jamais l'exécution. Chaque exécution exige une
autorisation distincte, à usage unique, liée au contenu, au profil et à l'image.</p>
{table(["Autorisation", "Profil", "Image", "Contenu (SHA-256)", "Par", "Motif", "Date", "État"], exec_rows,
       "Aucune autorisation d'exécution.")}</section>
<section><h2>Fichiers générés</h2><p class="muted">Lus depuis le manifeste enregistré, jamais exécutés dans
cette page. Le HTML et le JavaScript sont affichés comme texte.</p>
{table(["N°", "Chemin", "Taille", "Consulter"], file_rows, "Aucun fichier généré.")}</section>
<section><h2>Exécutions isolées</h2>{table(["Tentative", "Type", "Verdict", "Résumé déclaré (non fiable)", "Début"],
                                            run_rows, "Aucune exécution isolée.")}</section>"""
        return page(settings, "Tâche", body, "/tasks", now)

    @app.get("/tasks/{task_id}/files/{number}")
    def task_file(task_id: str, number: int, download: int = Query(0, ge=0, le=1)):
        now = datetime.now(timezone.utc)
        valid_id(task_id)
        with sessions() as db:
            task = db.get(Task, task_id)
            if task is None:
                raise HTTPException(status_code=404)
        manifest = loads(task.artifact_manifest, {})
        files = manifest.get("files") if isinstance(manifest, dict) and isinstance(manifest.get("files"), list) else []
        if not 0 <= number < min(len(files), 50) or not isinstance(files[number], dict):
            raise HTTPException(status_code=404)
        path, content = str(files[number].get("path", "")), str(files[number].get("content", ""))
        if download:
            # text/plain + nosniff + sandbox CSP: the browser never renders generated HTML/JS here.
            return PlainTextResponse(content[:TEXT_MAX_CHARS], headers={
                "Content-Disposition": f'attachment; filename="prototype-{task_id[:10]}-{number}.txt"',
                "Content-Security-Policy": "sandbox; default-src 'none'"})
        body = (f"<h1>Fichier <code>{clip(path, 200)}</code></h1><p><a href='/tasks/{h(task_id)}'>← Tâche</a> · "
                f"<a href='?download=1'>Télécharger en texte</a></p><div class='notice'>Contenu généré par un "
                f"modèle : affiché comme texte échappé, jamais exécuté.</div><pre>{visible(content[:TEXT_MAX_CHARS])}</pre>")
        return page(settings, "Fichier", body, "/tasks", now)

    # ---------------------------------------------------------- work queue
    @app.get("/work", response_class=HTMLResponse)
    def work(page_number: int = Query(1, alias="page", ge=1, le=MAX_PAGE), state: str | None = Query(None)):
        now = datetime.now(timezone.utc)
        state = state if state in WORK_STATES else None
        with sessions() as db:
            query = select(WorkItem) if state is None else select(WorkItem).where(WorkItem.state == state)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            rows = db.scalars(query.order_by(WorkItem.scheduled_at.desc()).offset(offset(page_number))
                              .limit(PAGE_SIZE)).all()
        filters = "".join(f'<a href="/work?state={s}"{" aria-current=true" if s == state else ""}>{s}</a>'
                          for s in WORK_STATES)
        lines = [f"<tr><td><a href='/work/{h(w.id)}'>{short(w.id)}</a></td><td>{h(w.kind)}</td><td>{tag(w.state)}</td>"
                 f"<td>{w.priority}</td><td>{h(ts(w.scheduled_at))}</td><td>{w.attempts} / {w.max_attempts}</td>"
                 f"<td>{h(w.owner or '—')}<br><span class='muted'>{h(ts(w.lease_until))}</span></td>"
                 f"<td>{work_action(w)}</td></tr>" for w in rows]
        body = ("<h1>File de travaux exécutables</h1><p class='muted'>Distincte des tickets métier : un travail est "
                "une exécution planifiée (réunion ou génération MOCK) avec lease, tentatives bornées et historique.</p>"
                f"<div class='filters'><a href='/work'{' aria-current=true' if state is None else ''}>Tous</a>{filters}</div>"
                + table(["Travail", "Type", "État", "Priorité", "Planifié", "Tentatives", "Worker / lease",
                         "Prochaine action"], lines, "Aucun travail sur cette page.")
                + pager("/work", page_number, total, {"state": state}))
        return page(settings, "File", f"<section>{body}</section>", "/work", now)

    @app.get("/work/{work_id}", response_class=HTMLResponse)
    def work_detail(work_id: str):
        now = datetime.now(timezone.utc)
        valid_id(work_id)
        with sessions() as db:
            item = db.get(WorkItem, work_id)
            if item is None:
                raise HTTPException(status_code=404)
            attempts = db.scalars(select(Attempt).where(Attempt.work_id == work_id)
                                  .order_by(Attempt.started_at).limit(100)).all()
            reviews = db.scalars(select(WorkReview).where(WorkReview.work_id == work_id)
                                 .order_by(WorkReview.created_at)).all()
            events = db.scalars(select(AuditEvent).where(AuditEvent.entity_id == work_id)
                                .order_by(AuditEvent.created_at).limit(100)).all()
        attempt_rows = [f"<tr><td>{short(a.id)}</td><td>{h(a.owner)}</td><td>{h(ts(a.started_at))}</td>"
                        f"<td>{h(ts(a.finished_at))}</td><td>{tag(a.outcome)}</td></tr>" for a in attempts]
        review_rows = [f"<tr><td>{h(ts(r.created_at))}</td><td>{h(r.actor)}</td><td>{tag(r.previous_state)}</td>"
                       f"<td>{clip(r.reason, 300)}</td></tr>" for r in reviews]
        event_rows = [f"<tr><td>{h(ts(e.created_at))}</td><td>{h(e.kind)}</td></tr>" for e in events]
        linked = (f"<a href='/tasks/{h(item.task_id)}'>{short(item.task_id)}</a>" if item.task_id
                  else f"<a href='/runs/{h(item.run_id)}'>réunion {short(item.run_id)}</a>")
        body = f"""<h1>Travail {short(item.id)}</h1>
<section><dl><dt>Type</dt><dd>{h(item.kind)}</dd><dt>État</dt><dd>{tag(item.state)}</dd>
<dt>Lié à</dt><dd>{linked}</dd><dt>Priorité</dt><dd>{item.priority}</dd><dt>Planifié</dt><dd>{h(ts(item.scheduled_at))}</dd>
<dt>Tentatives</dt><dd>{item.attempts} / {item.max_attempts}</dd><dt>Worker</dt><dd>{h(item.owner or '—')}</dd>
<dt>Lease jusqu'à</dt><dd>{h(ts(item.lease_until))}</dd>
<dt>Effet externe</dt><dd>{'oui : jamais rejoué automatiquement' if item.external else 'non'}</dd></dl></section>
<section><h2>Prochaine action humaine</h2>{work_action(item)}</section>
<section><h2>Tentatives</h2>{table(["Tentative", "Worker", "Début", "Fin", "Issue"], attempt_rows, "Aucune tentative.")}</section>
<section><h2>Revues humaines</h2>{table(["Date", "Par", "État précédent", "Motif"], review_rows, "Aucune revue.")}</section>
<section><h2>Journal d'audit</h2>{table(["Date", "Événement"], event_rows, "Aucun événement.")}</section>"""
        return page(settings, "Travail", body, "/work", now)

    # ---------------------------------------------------------- sandbox
    @app.get("/sandbox", response_class=HTMLResponse)
    def sandbox_list(page_number: int = Query(1, alias="page", ge=1, le=MAX_PAGE)):
        now = datetime.now(timezone.utc)
        with sessions() as db:
            total = db.scalar(select(func.count()).select_from(SandboxRun))
            rows = db.scalars(select(SandboxRun).order_by(SandboxRun.started_at.desc())
                              .offset(offset(page_number)).limit(PAGE_SIZE)).all()
        lines = [f"<tr><td><a href='/sandbox/{h(s.id)}'>{short(s.id)}</a></td>"
                 f"<td><a href='/tasks/{h(s.task_id)}'>{short(s.task_id)}</a> #{s.attempt}</td>"
                 f"<td>{h(profile_label(s.profile))}</td><td>{tag(s.state)}</td><td>{s.exit_code if s.exit_code is not None else '—'}</td>"
                 f"<td>{h(summary_text(s.summary_json))}</td><td>{h(ts(s.started_at))}</td></tr>" for s in rows]
        body = ("<h1>Exécutions isolées (sandbox Docker)</h1><div class='notice'>Les rapports du conteneur sont non "
                "fiables. Un résultat technique n'est jamais une validation commerciale.</div>"
                + table(["Exécution", "Tâche", "Type", "Verdict", "Code", "Résumé déclaré (non fiable)", "Début"], lines,
                        "Aucune exécution isolée.") + pager("/sandbox", page_number, total))
        return page(settings, "Sandbox", f"<section>{body}</section>", "/sandbox", now)

    @app.get("/sandbox/{run_id}", response_class=HTMLResponse)
    def sandbox_detail(run_id: str):
        now = datetime.now(timezone.utc)
        valid_id(run_id)
        with sessions() as db:
            run = db.get(SandboxRun, run_id)
            if run is None:
                raise HTTPException(status_code=404)
            approval = db.get(ExecutionApproval, run.approval_id)
            cleanups = db.scalars(select(SandboxCleanup).where(SandboxCleanup.run_id == run_id)
                                  .order_by(SandboxCleanup.created_at)).all()
            evidence = db.scalar(select(SandboxEvidence.created_at).where(SandboxEvidence.run_id == run_id))
        cleanup_rows = [f"<tr><td>{h(ts(c.created_at))}</td><td>{h(c.actor)}</td><td>{tag(c.outcome)}</td>"
                        f"<td>{h(c.previous_state or '—')}</td><td>{'oui' if c.container_removed else 'non'}</td>"
                        f"<td>{'oui' if c.workspace_removed else 'non'}</td><td>{clip(c.reason, 200)}</td></tr>"
                        for c in cleanups]
        late = "résultat tardif" in (run.detail or "")
        actions = ""
        if run.state == "RUNNING":
            actions = (command("python -m company sandbox doctor",
                               "Diagnostic en lecture seule : conteneur actif, orphelin ou en démarrage.")
                       + command(f'python -m company sandbox cleanup --run {run.id} --actor VOTRE_NOM --reason "motif"',
                                 "Nettoyage ciblé audité ; refusé si le conteneur est encore actif. Ne relance "
                                 "jamais le test et ne consomme aucune autorisation."))
        body = f"""<h1>Exécution isolée {short(run.id)}</h1>
<div class="notice">Rapport du conteneur non fiable. Ce résultat technique n'est ni une validation commerciale
ni une mesure globale de qualité.</div>
<section><dl><dt>Tâche</dt><dd><a href='/tasks/{h(run.task_id)}'>{short(run.task_id)}</a> · tentative {run.attempt}</dd>
<dt>Type</dt><dd>{h(profile_label(run.profile))}</dd><dt>Verdict</dt><dd>{tag(run.state)} (code {h(run.exit_code)})</dd>
<dt>Résumé déclaré</dt><dd>{h(summary_text(run.summary_json))}</dd>
<dt>Image exacte</dt><dd><code>{h(run.image_id)}</code></dd>
<dt>Version du validateur</dt><dd><code>{h(run.validator_version or '—')}</code></dd>
<dt>Contenu (SHA-256)</dt><dd><code>{h(run.manifest_sha256)}</code></dd>
<dt>Autorisation</dt><dd>{(h(approval.actor) + ' · ' + clip(approval.reason, 200)) if approval else '—'}</dd>
<dt>Durée</dt><dd>{h(run.duration_ms if run.duration_ms is not None else '—')} ms · délai {run.timeout_s} s</dd>
<dt>Sorties</dt><dd>{run.output_bytes} octets{' (tronquées)' if run.truncated else ''}</dd>
<dt>Preuve conservée</dt><dd>{h(ts(evidence)) if evidence else '—'}</dd>
<dt>Détail</dt><dd>{clip(run.detail, 250)}{' — résultat arrivé après interruption' if late else ''}</dd></dl></section>
{f'<section><h2>Intervention possible</h2>{actions}</section>' if actions else ''}
<section><h2>Nettoyages audités</h2>{table(["Date", "Par", "Résultat", "État précédent", "Conteneur supprimé",
                                             "Espace supprimé", "Motif"], cleanup_rows, "Aucun nettoyage.")}</section>
<section><h2>Sortie standard (non fiable)</h2><pre>{visible(run.stdout[:TEXT_MAX_CHARS])}</pre></section>
<section><h2>Sortie d'erreur (non fiable)</h2><pre>{visible(run.stderr[:TEXT_MAX_CHARS])}</pre></section>"""
        return page(settings, "Exécution isolée", body, "/sandbox", now)

    # ---------------------------------------------------------- budget
    @app.get("/budget", response_class=HTMLResponse)
    def budget_page(page_number: int = Query(1, alias="page", ge=1, le=MAX_PAGE), state: str | None = Query(None)):
        now = datetime.now(timezone.utc)
        state = state if state in LEDGER_STATES else None
        with sessions() as db:
            summary = db.execute(select(Ledger.state, func.count(), func.coalesce(func.sum(Ledger.amount_eur), 0),
                                        func.sum(Ledger.estimated_eur), func.sum(Ledger.actual_eur))
                                 .group_by(Ledger.state)).all()
            query = select(Ledger) if state is None else select(Ledger).where(Ledger.state == state)
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            rows = db.scalars(query.order_by(Ledger.created_at.desc()).offset(offset(page_number))
                              .limit(PAGE_SIZE)).all()
        by_state = {s: (n, amount, est, act) for s, n, amount, est, act in summary}
        cards = "".join(
            f"<div class='card'><div class='muted'>{h(label)}</div><div class='big'>{eur(by_state.get(key, (0, 0))[1])}</div>"
            f"<div class='muted'>{by_state.get(key, (0,))[0]} écriture(s)</div><p class='muted'>{h(note)}</p></div>"
            for key, (label, note) in LEDGER_STATES.items())
        filters = "".join(f'<a href="/budget?state={k}"{" aria-current=true" if k == state else ""}>{h(v[0])}</a>'
                          for k, v in LEDGER_STATES.items())
        lines = [f"<tr id='l-{h(x.id)}'><td><code>{short(x.id)}</code></td><td><a href='/runs/{h(x.run_id)}'>{short(x.run_id)}</a></td>"
                 f"<td>{h(x.provider)}</td><td>{tag(x.state)} {h(LEDGER_STATES.get(x.state, (x.state,))[0])}</td>"
                 f"<td>{eur(x.estimated_eur)}</td><td>{eur(x.actual_eur)}</td><td><strong>{eur(x.amount_eur)}</strong></td>"
                 f"<td>{clip(x.reconciliation_reference or '—', 120)}</td><td>{h(ts(x.created_at))}</td></tr>" for x in rows]
        accounted = sum((Decimal(v[1]) for v in by_state.values()), Decimal(0))
        body = f"""<h1>Budget et coûts</h1>
<div class="grid"><div class="card"><div class="muted">Plafond mensuel configuré</div><div class="big">{h(settings.monthly_limit)} €</div></div>
<div class="card"><div class="muted">Plafond par réunion</div><div class="big">{h(settings.run_limit)} €</div></div>
<div class="card"><div class="muted">Total comptabilisé</div><div class="big">{eur(accounted)}</div></div></div>
<div class="notice">Un coût calculé depuis les tokens est une estimation locale avec les tarifs configurés,
pas une facture de l'opérateur. Seuls les montants rapprochés portent une référence vérifiée par un humain.</div>
<div class="grid">{cards}</div>
<section><h2>Écritures</h2><div class='filters'><a href='/budget'{' aria-current=true' if state is None else ''}>Toutes</a>{filters}</div>
{table(["Écriture", "Réunion", "Fournisseur", "Catégorie", "Estimation", "Calculé / rapproché", "Comptabilisé",
        "Référence", "Date"], lines, "Aucune écriture.")}{pager("/budget", page_number, total, {"state": state})}</section>"""
        return page(settings, "Budget", body, "/budget", now)
