"""Configuration stricte : mode démo par défaut, pas de tarification inventée."""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent


def dec(name: str, default: str = "0") -> Decimal:
    try:
        value = Decimal(os.getenv(name, default) or default)
        if not value.is_finite() or value < 0:
            raise ValueError(f"{name} doit être un nombre positif")
        return value
    except InvalidOperation as exc:
        raise ValueError(f"{name} est invalide") from exc


@dataclass(frozen=True)
class Settings:
    mock: bool
    mission: str
    db_url: str
    monthly_limit: Decimal
    run_limit: Decimal
    max_output_tokens: int
    models: dict[str, str]
    keys: dict[str, str]
    prices: dict[str, tuple[Decimal, Decimal]]
    tavily_key: str
    tavily_search_price: Decimal
    timezone: str
    schedule_hour: int
    schedule_minute: int

    @classmethod
    def load(cls) -> "Settings":
        mock = os.getenv("MOCK_MODE", "true").lower() in ("true", "yes", "1")
        providers = ("openai", "anthropic", "gemini")
        settings = cls(
            mock=mock,
            mission=os.getenv("MISSION", "Étudier des opportunités entrepreneuriales viables.").strip(),
            db_url=os.getenv("DATABASE_URL", f"sqlite:///{ROOT}/data/company.db"),
            monthly_limit=dec("MONTHLY_BUDGET_EUR", "100"),
            run_limit=dec("MAX_RUN_BUDGET_EUR", "3"),
            max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "500")),
            models={p: os.getenv(f"{p.upper()}_MODEL", "").strip() for p in providers},
            keys={p: os.getenv(f"{p.upper()}_API_KEY", "").strip() for p in providers},
            prices={p: (dec(f"{p.upper()}_INPUT_EUR_PER_MTOK"), dec(f"{p.upper()}_OUTPUT_EUR_PER_MTOK")) for p in providers},
            tavily_key=os.getenv("TAVILY_API_KEY", "").strip(),
            tavily_search_price=dec("TAVILY_COST_EUR_PER_SEARCH"),
            timezone=os.getenv("TIMEZONE", "Europe/Paris"),
            schedule_hour=int(os.getenv("SCHEDULE_HOUR", "9")),
            schedule_minute=int(os.getenv("SCHEDULE_MINUTE", "0")),
        )
        if not settings.mission or len(settings.mission) > 2000:
            raise ValueError("MISSION doit contenir entre 1 et 2000 caractères")
        if not 100 <= settings.max_output_tokens <= 4000:
            raise ValueError("MAX_OUTPUT_TOKENS doit être entre 100 et 4000")
        if not 0 <= settings.schedule_hour < 24 or not 0 <= settings.schedule_minute < 60:
            raise ValueError("Horaires invalides")
        if settings.monthly_limit <= 0 or settings.run_limit <= 0:
            raise ValueError("Les limites budgétaires doivent être positives")
        if not mock:
            for p in providers:
                if not settings.keys[p] or not settings.models[p] or min(settings.prices[p]) <= 0:
                    raise ValueError(f"Mode réel : clé API, identifiant modèle et 2 tarifs requis pour {p}")
            if settings.tavily_key and settings.tavily_search_price <= 0:
                raise ValueError("Renseigne TAVILY_COST_EUR_PER_SEARCH avant toute recherche payante")
        return settings
