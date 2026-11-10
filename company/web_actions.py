"""Opt-in local administrative actions. No write is exposed without explicit credentials and permissions.

Never bind the dashboard to a public address. Each operation uses the same backend
invariants as the CLI; a web approval is not an execution approval.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape as h
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from starlette.concurrency import run_in_threadpool

from .supervision import page
from .database import Task, WorkItem, Ledger

COOKIE = "aac_web_session"
ACTOR = "web:admin"
MAX_BODY = 8192
ID = re.compile(r"^[0-9a-f]{32}$")
PERMISSIONS = frozenset({
    "approve", "prototype", "requeue", "reconcile", "sandbox_approve", "sandbox_run",
})
LABELS = {
    "approve": "Autoriser la génération (ne lance rien)",
    "prototype": "Générer les fichiers du prototype (sans exécution)",
    "requeue": "Réactiver une tâche MOCK échouée",
    "reconcile": "Rapprocher un coût vérifié",
    "sandbox_approve": "Autoriser UNE exécution sandbox du contenu relu",
    "sandbox_run": "Consommer l'autorisation et lancer le test Docker isolé",
}


def configuration():
    token = os.getenv("AAC_WEB_ADMIN_TOKEN", "")
    key = os.getenv("AAC_WEB_SESSION_SECRET", "")
    if not token and not key:
        return None
    if len(token) < 32 or len(key) < 32 or token == key:
        raise RuntimeError("Actions web : deux secrets distincts de 32 caractères minimum requis")
    requested = set(filter(None, (p.strip() for p in os.getenv("AAC_WEB_PERMISSIONS", "").split(","))))
    if not requested <= PERMISSIONS:
        raise RuntimeError("Actions web : permission inconnue")
    return token, key.encode("utf-8"), requested


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _cookie(secret: bytes, token: str) -> str:
    payload = json.dumps({"expires": int(time.time()) + 3600, "csrf": secrets.token_urlsafe(32)},
                         separators=(",", ":")).encode("utf-8")
    body = _encode(payload)
    signing_key = hmac.digest(secret, token.encode("utf-8"), "sha256")
    signature = _encode(hmac.digest(signing_key, body.encode("ascii"), "sha256"))
    return body + "." + signature


def _session(request: Request, cfg):
    if cfg is None:
        raise HTTPException(404, "Actions désactivées")
    raw = request.cookies.get(COOKIE, "")
    if len(raw) > 1024 or "." not in raw:
        return None
    try:
        body, signature = raw.split(".", 1)
        signing_key = hmac.digest(cfg[1], cfg[0].encode("utf-8"), "sha256")
        expected = _encode(hmac.digest(signing_key, body.encode("ascii"), "sha256"))
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_decode(body))
        if not isinstance(claims.get("expires"), int) or claims["expires"] < time.time():
            return None
        if not isinstance(claims.get("csrf"), str) or len(claims["csrf"]) < 32:
            return None
        return claims
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return None


def _same_origin(request: Request):
    # Fail closed. Browser form submissions normally include Origin; some use Referer.
    host = request.headers.get("host", "").lower()
    if host not in ("127.0.0.1:8000", "localhost:8000", "127.0.0.1", "localhost", "testserver"):
        raise HTTPException(403, "Hôte non autorisé")
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    allowed = f"{request.url.scheme}://{host}"
    if origin is not None:
        if origin != allowed:
            raise HTTPException(403, "Origine interdite")
    elif referer is None or not (referer == allowed or referer.startswith(allowed + "/")):
        raise HTTPException(403, "Origine requise")


async def _fields(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Formulaire requis")
    try:
        declared_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        raise HTTPException(400, "Taille invalide")
    if declared_length > MAX_BODY:
        raise HTTPException(413, "Formulaire trop volumineux")
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Formulaire trop volumineux")
    try:
        params = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True,
                          max_num_fields=15, strict_parsing=True)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Formulaire invalide")
    if any(len(v) != 1 for v in params.values()):
        raise HTTPException(400, "Champs dupliqués")
    return {key: value[0] for key, value in params.items()}


def _identifier(raw: str) -> str:
    if not ID.fullmatch(raw or ""):
        raise HTTPException(400, "Identifiant invalide")
    return raw


def _form(kind: str, csrf: str, fields: str, *, note: str) -> str:
    return (f'<section><h2>{h(LABELS[kind])}</h2><p class="muted">{h(note)}</p>'
            f'<form action="/actions/{kind}" method="post">'
            f'<input type="hidden" name="csrf" value="{h(csrf)}">'
            f'{fields}<p><button type="submit">{h(LABELS[kind])}</button></p></form></section>')


def _field(name: str, label: str, *, required=True, typ="text", maxlength=500) -> str:
    return (f'<label style="display:block;margin:12px 0">{h(label)} '
            f'<input name="{h(name)}" type="{h(typ)}" maxlength="{maxlength}" '
            f'{"required" if required else ""} style="width:100%;padding:9px"></label>')


def register(app: FastAPI, sessions, settings, budget) -> None:
    # Sample configuration at startup. Disabled by default, no route changes to the read-only API.
    cfg = configuration()

    @app.get("/actions", response_class=HTMLResponse)
    def index(request: Request):
        if cfg is None:
            return page(settings, "Actions désactivées", "<h1>Actions désactivées</h1><p>Configurer les secrets et permissions pour activer les actions web.</p>", "/actions", datetime.now(timezone.utc))
        identity = _session(request, cfg)
        if identity is None:
            body = ('<h1>Connexion administrateur</h1><p>Console locale : accès protégé.</p>'
                    '<form method="post" action="/actions/login">'
                    + _field("token", "Jeton administrateur", typ="password", maxlength=512)
                    + '<button type="submit">Se connecter</button></form>')
            return page(settings, "Connexion", body, "/actions", datetime.now(timezone.utc))
        csrf = identity["csrf"]
        perms = cfg[2]
        body = '<h1>Actions administrateur</h1><p>Chaque opération est indépendante et auditée. Aucun appel API payant depuis cette page.</p>'
        if request.query_params.get("done") in PERMISSIONS:
            body += '<p role="status">Opération enregistrée. Vérifiez l’état dans la supervision.</p>'
        task = _field("id", "Identifiant de tâche", maxlength=32)
        item = _field("id", "Identifiant du travail en file", maxlength=32)
        if "approve" in perms:
            body += _form("approve", csrf, task, note="Accord de génération uniquement, sans exécution.")
        if "prototype" in perms and settings.mock:
            body += _form("prototype", csrf, task, note="Mode MOCK uniquement ; écrit des fichiers locaux, ne les exécute pas.")
        if "requeue" in perms:
            body += _form("requeue", csrf, item + _field("reason", "Motif (obligatoire)", maxlength=500),
                          note="Réactivation limitée à une tâche MOCK en échec ou en attente humaine.")
        if "reconcile" in perms:
            body += _form("reconcile", csrf,
                          _field("id", "Identifiant d’écriture", maxlength=32)
                          + _field("amount", "Coût vérifié en EUR (ex. 0.012300)", maxlength=36)
                          + _field("reference", "Référence du relevé vérifié", maxlength=500),
                          note="N'autorise jamais un nouvel appel fournisseur.")
        if "sandbox_approve" in perms:
            body += _form("sandbox_approve", csrf, task
                          + _field("profile", "Profil autorisé (static-html ou python-unittest)", maxlength=30)
                          + _field("reason", "Motif de la revue", maxlength=500)
                          + '<label><input name="reviewed" type="checkbox" value="yes" required> '
                            'J’ai relu le contenu exact généré</label>',
                          note="Autorisation unique liée au SHA-256 du contenu et à l’image Docker autorisée.")
        if "sandbox_run" in perms:
            body += _form("sandbox_run", csrf, task
                          + '<label><input name="confirmed" type="checkbox" value="yes" required> '
                            'Je confirme la consommation de l’autorisation d’exécution</label>',
                          note="Lance les tests Docker isolés uniquement si l’autorisation est valide et inutilisée.")
        body += (f'<section><h2>Session</h2><form method="post" action="/actions/logout">'
                 f'<input type="hidden" name="csrf" value="{h(csrf)}">'
                 f'<button type="submit">Déconnexion</button></form></section>')
        return page(settings, "Actions", body, "/actions", datetime.now(timezone.utc))

    @app.post("/actions/login")
    async def login(request: Request):
        if cfg is None:
            raise HTTPException(404, "Actions désactivées")
        _same_origin(request)
        fields = await _fields(request)
        if not hmac.compare_digest(fields.get("token", ""), cfg[0]):
            # Deliberately do not distinguish unknown user, expired token or password.
            raise HTTPException(403, "Authentification refusée")
        response = RedirectResponse("/actions", status_code=303)
        response.set_cookie(COOKIE, _cookie(cfg[1], cfg[0]), max_age=3600, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/actions")
        return response

    def authorized(request: Request, fields: dict, kind: str):
        if cfg is None:
            raise HTTPException(404, "Actions désactivées")
        if kind not in cfg[2]:
            raise HTTPException(403, "Permission absente")
        session = _session(request, cfg)
        if session is None:
            raise HTTPException(401, "Connexion requise")
        if not hmac.compare_digest(fields.get("csrf", ""), session["csrf"]):
            raise HTTPException(403, "Jeton CSRF invalide")

    @app.post("/actions/logout")
    async def logout(request: Request):
        if cfg is None:
            raise HTTPException(404)
        _same_origin(request)
        fields = await _fields(request)
        if cfg is None or (session := _session(request, cfg)) is None:
            raise HTTPException(401, "Connexion requise")
        if not hmac.compare_digest(fields.get("csrf", ""), session["csrf"]):
            raise HTTPException(403, "Jeton CSRF invalide")
        response = RedirectResponse("/actions", status_code=303)
        response.delete_cookie(COOKIE, path="/actions")
        return response

    @app.post("/actions/{kind}")
    async def action(kind: str, request: Request):
        if cfg is None or kind not in PERMISSIONS:
            raise HTTPException(404)
        _same_origin(request)
        fields = await _fields(request)
        authorized(request, fields, kind)
        identifier = _identifier(fields.get("id", ""))
        try:
            # Validate permissions BEFORE loading providers or triggering any side-effect.
            if kind == "approve":
                from .approvals import approve_task
                await run_in_threadpool(approve_task, sessions, identifier, actor=ACTOR)
            elif kind == "prototype":
                if not settings.mock:
                    raise ValueError("Génération web limitée au mode MOCK")
                from .prototyping import generate_prototype
                await run_in_threadpool(generate_prototype, sessions, settings, budget, identifier)
            elif kind == "requeue":
                from .queue import Queue
                await run_in_threadpool(Queue(sessions).requeue, identifier, actor=ACTOR,
                                        reason=fields.get("reason", ""))
            elif kind == "reconcile":
                from .money import money
                reference = fields.get("reference", "")
                raw = fields.get("amount", "")
                if len(raw) > 36 or not re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?", raw):
                    raise ValueError("Montant EUR positif ou nul, avec six décimales maximum")
                await run_in_threadpool(budget.reconcile_unknown, identifier, money(Decimal(raw)), reference)
            elif kind == "sandbox_approve":
                if fields.get("reviewed") != "yes":
                    raise ValueError("Relecture explicite requise")
                from . import sandbox
                profile = fields.get("profile", "")
                image = sandbox.allowed_images().get(profile)
                if image is None:
                    raise ValueError("Profil ou image non autorisé")
                await run_in_threadpool(sandbox.approve_execution, sessions, identifier, profile=profile,
                                        image_id=image, actor=ACTOR, reason=fields.get("reason", ""))
            elif kind == "sandbox_run":
                if fields.get("confirmed") != "yes":
                    raise ValueError("Confirmation d’exécution requise")
                from . import sandbox
                await run_in_threadpool(sandbox.run_approved, sessions, identifier)
        except (ValueError, InvalidOperation) as exc:
            # No stacktrace, DSN or content of third-party errors in browser.
            raise HTTPException(409, "Action refusée : état, validation ou paramètres incompatibles")
        return RedirectResponse(f"/actions?done={kind}", status_code=303)
