"""Recherche extérieure facultative et bornée : sources affichées, jamais d'exécution de leur contenu."""
from __future__ import annotations

import httpx


class SearchService:
    def __init__(self, settings, budget):
        self.settings, self.budget = settings, budget

    def search(self, run_id: str, query: str) -> list[dict]:
        if self.settings.mock or not self.settings.tavily_key:
            return []
        reservation = self.budget.reserve(run_id, "tavily", self.settings.tavily_search_price, "web_search")
        try:
            with httpx.Client(timeout=20) as client:
                response = client.post("https://api.tavily.com/search", json={
                    "api_key": self.settings.tavily_key,
                    "query": query[:220], "search_depth": "basic", "max_results": 3,
                    "include_answer": False, "include_raw_content": False,
                })
                response.raise_for_status()
                results = response.json().get("results", [])
            self.budget.settle(reservation, self.settings.tavily_search_price)
            return [{"title": str(r.get("title", ""))[:120], "url": str(r.get("url", ""))[:300],
                     "snippet": str(r.get("content", ""))[:600]} for r in results[:3]]
        except Exception:
            self.budget.settle(reservation, None, error=True)
            raise
