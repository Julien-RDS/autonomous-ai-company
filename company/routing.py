"""Routage empirique : métriques observables, pas de prétendue intelligence auto-évaluée."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from .database import Message, Task


PROVIDERS = ("openai", "anthropic", "gemini")


def rank_providers(sessions, category: str = "prototype") -> list[dict]:
    with sessions() as db:
        # La validation humaine d'un prototype peut fournir un signal de réussite explicite.
        tasks = db.scalars(select(Task).where(Task.category == category, Task.success.is_not(None))).all()
        messages = db.scalars(select(Message).where(Message.stage == category)).all()
    counts = defaultdict(lambda: {"done": 0, "wins": 0, "latency_ms": [], "errors": 0})
    for t in tasks:
        if t.provider in PROVIDERS:
            counts[t.provider]["done"] += 1
            counts[t.provider]["wins"] += bool(t.success)
    for msg in messages:
        if msg.provider in PROVIDERS:
            counts[msg.provider]["latency_ms"].append(msg.latency_ms)
            counts[msg.provider]["errors"] += int(not msg.success)
    result = []
    for p in PROVIDERS:
        c = counts[p]
        # Les agents sans évaluation humaine ne sont pas artificiellement déclarés meilleurs.
        score = (c["wins"] / c["done"] if c["done"] else None)
        mean_latency = (sum(c["latency_ms"]) / len(c["latency_ms"]) if c["latency_ms"] else None)
        result.append({"provider": p, "samples": c["done"], "success_rate": score,
                       "average_latency_ms": round(mean_latency) if mean_latency is not None else None,
                       "errors": c["errors"]})
    return sorted(result, key=lambda x: (-(x["success_rate"] if x["success_rate"] is not None else -1),
                                      -x["samples"], x["errors"], x["provider"]))


def select_provider(sessions, category: str = "prototype") -> str:
    ranking = rank_providers(sessions, category)
    # Aucun historique de qualité fiable : rotation déterministe pour explorer.
    eligible = [row for row in ranking if row["samples"] >= 3]
    if eligible:
        return eligible[0]["provider"]
    with sessions() as db:
        count = len(db.scalars(select(Message).where(Message.stage == category)).all())
    return PROVIDERS[count % len(PROVIDERS)]
