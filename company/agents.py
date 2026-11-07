"""Adaptateurs API officiels ; le mode démo ne fait aucun appel réseau."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .database import Message


logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Public failure without SDK payloads, URLs or credentials."""


def safe_error(exc: Exception) -> str:
    # SDK exception text can contain request credentials or response bodies.
    return type(exc).__name__


@dataclass
class Result:
    text: str
    input_tokens: int
    output_tokens: int


MOCK_OPPORTUNITIES = {
    "openai": {
        "title": "Audit de présence web pour artisans",
        "problem": "Des professionnels ont peu de temps pour corriger leur visibilité locale.",
        "customer": "Artisans indépendants",
        "validation_query": "audit présence web artisan indépendant concurrence France",
        "experiment": "Interroger 5 artisans volontaires et tester une démonstration gratuite",
        "success_metric": "2 intérêts qualifiés sur 5 entretiens",
    },
    "anthropic": {
        "title": "Mini-outil de relance de devis",
        "problem": "Les petites entreprises oublient certaines relances commerciales.",
        "customer": "Petites entreprises de services",
        "validation_query": "outils relance devis PME artisans prix concurrence",
        "experiment": "Faire valider une maquette par 5 entreprises",
        "success_metric": "3 confirmations de besoin parmi 5 entreprises",
    },
    "gemini": {
        "title": "Tableau de suivi énergétique TPE",
        "problem": "Les très petites structures manquent de visibilité sur leurs consommations.",
        "customer": "TPE avec locaux",
        "validation_query": "logiciel suivi énergie TPE marché concurrence France",
        "experiment": "Tester une feuille de calcul simulée avec 5 responsables",
        "success_metric": "2 utilisateurs prêts à tester un outil",
    },
}


class ProviderClient:
    def __init__(self, settings):
        self.settings = settings

    def generate(self, provider: str, stage: str, prompt: str) -> Result:
        if self.settings.mock:
            return self._mock(provider, stage)
        model = self.settings.models[provider]
        key = self.settings.keys[provider]
        limit = self.settings.max_output_tokens
        if provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=key, timeout=45, max_retries=0)
            response = client.responses.create(
                model=model, instructions="Tu es membre d'un comité d'IA. Réponds en français et respecte strictement le format demandé.",
                input=prompt, max_output_tokens=limit, store=False,
            )
            usage = response.usage
            return Result(response.output_text or "", getattr(usage, "input_tokens", 0) or 0,
                          getattr(usage, "output_tokens", 0) or 0)
        if provider == "anthropic":
            from anthropic import Anthropic
            client = Anthropic(api_key=key, timeout=45, max_retries=0)
            response = client.messages.create(
                model=model, max_tokens=limit,
                system="Tu es membre d'un comité d'IA. Réponds en français et respecte strictement le format demandé.",
                messages=[{"role": "user", "content": prompt}],
            )
            text = "\n".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            usage = response.usage
            # Cache inclus au prix d'entrée normal : surestimation conservatrice possible.
            in_tok = ((usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) +
                      (getattr(usage, "cache_read_input_tokens", 0) or 0))
            return Result(text, in_tok, usage.output_tokens or 0)
        if provider == "gemini":
            from google import genai
            from google.genai import types
            with genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=45_000, retry_options=types.HttpRetryOptions(attempts=1),
            )) as client:
                response = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction="Tu es membre d'un comité d'IA. Réponds en français et respecte strictement le format demandé.",
                        max_output_tokens=limit,
                        automatic_continuation=False,
                    ),
                )
            usage = response.usage_metadata
            inp = getattr(usage, "prompt_token_count", 0) or 0
            total = getattr(usage, "total_token_count", None)
            out = max(0, total - inp) if total is not None else (
                (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0))
            return Result(response.text or "", inp, out)
        raise ValueError(f"Fournisseur inconnu : {provider}")

    @staticmethod
    def _mock(provider: str, stage: str) -> Result:
        if stage == "proposal":
            text = json.dumps(MOCK_OPPORTUNITIES[provider], ensure_ascii=False)
        elif stage == "critique":
            text = ("Les hypothèses restent à vérifier auprès de clients réels. "
                    "Le positionnement et le prix doivent être comparés à la concurrence. "
                    "Ne déduisez aucune demande validée de ce débat fictif.")
        elif stage == "vote":
            # Le vote démo est artificiel et n'a pas valeur d'évaluation de marché.
            selected = {"openai": "anthropic", "anthropic": "anthropic", "gemini": "openai"}[provider]
            text = json.dumps({
                "proposal_id": selected,
                "reason": "Démonstration : test terrain simple et mesurable (ce vote est simulé).",
                "next_action": "Rédiger un guide d'entretien client et une maquette sans contact commercial automatisé",
            }, ensure_ascii=False)
        elif stage == "prototype":
            text = json.dumps({"files": [
                {"path": "README.md", "content": "# Démonstrateur fictif\n\nPrototype local. Aucun client réel ni marché validé.\n"},
                {"path": "index.html", "content": "<!doctype html><html lang='fr'><meta charset='utf-8'><title>Démo</title><body><h1>Prototype local</h1><p>Exemple statique.</p></body></html>"},
            ]}, ensure_ascii=False)
        else:
            raise ValueError(stage)
        return Result(text=text, input_tokens=0, output_tokens=0)


class AgentService:
    def __init__(self, sessions, settings, budget):
        self.sessions, self.settings, self.budget = sessions, settings, budget
        self.provider = ProviderClient(settings)

    def call(self, provider: str, stage: str, prompt: str, run_id: str) -> str:
        # Pas de conversation infinie : 1 appel par étape, sans retry automatique.
        prompt = prompt[:9000]
        if self.settings.mock:
            reservation = None
        else:
            estimate = self.budget.token_estimate(prompt, provider, self.settings.max_output_tokens)
            reservation = self.budget.reserve(run_id, provider, estimate, stage)
        started = time.monotonic()
        result = None
        error = None
        try:
            result = self.provider.generate(provider, stage, prompt)
            if not result.text.strip():
                raise ValueError("Réponse vide")
            return result.text
        except Exception as exc:
            error = exc
            logger.warning("provider_call_failed provider=%s stage=%s run_id=%s error=%s",
                           provider, stage, run_id, safe_error(exc))
            raise ProviderError(f"Échec du fournisseur {provider} ({safe_error(exc)})") from None
        finally:
            latency = round((time.monotonic() - started) * 1000)
            if reservation:
                # Une erreur réseau peut avoir généré des frais côté opérateur : garder la réservation.
                actual = (self.budget.actual_cost(provider, result.input_tokens, result.output_tokens)
                          if result is not None and result.input_tokens + result.output_tokens > 0 else None)
                self.budget.settle(reservation, actual, error is not None or actual is None)
            with self.sessions.begin() as db:
                db.add(Message(
                    run_id=run_id, stage=stage, provider=provider,
                    model="simulation" if self.settings.mock else self.settings.models[provider],
                    text=(result.text if result else f"Erreur {safe_error(error)}")[:20000],
                    input_tokens=result.input_tokens if result else 0,
                    output_tokens=result.output_tokens if result else 0,
                    latency_ms=latency, success=(error is None),
                ))
