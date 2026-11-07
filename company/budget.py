"""Réservation AVANT appel, écriture d'usage APRÈS appel (contrôle approximatif, non plafond opérateur)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select

from .database import Ledger


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, sessions, settings):
        self.sessions, self.settings = sessions, settings

    def totals(self, run_id: str | None = None) -> Decimal:
        with self.sessions() as db:
            q = select(func.coalesce(func.sum(Ledger.amount_eur), 0))
            if run_id:
                q = q.where(Ledger.run_id == run_id)
            else:
                now = datetime.now(timezone.utc)
                start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
                q = q.where(Ledger.created_at >= start)
            return Decimal(str(db.scalar(q) or 0))

    def reserve(self, run_id: str, provider: str, estimate: Decimal, details: str = "") -> str:
        if estimate < 0:
            raise ValueError("Estimation négative")
        # Une seule instance d'orchestrateur doit gérer le budget local.
        monthly = self.totals()
        one_run = self.totals(run_id)
        if monthly + estimate > self.settings.monthly_limit:
            raise BudgetExceeded(f"Plafond mensuel local atteint ({monthly:.4f} € comptabilisés)")
        if one_run + estimate > self.settings.run_limit:
            raise BudgetExceeded(f"Plafond de session atteint ({one_run:.4f} € comptabilisés)")
        with self.sessions.begin() as db:
            record = Ledger(run_id=run_id, provider=provider, amount_eur=float(estimate), details=details[:250])
            db.add(record)
            db.flush()
            return record.id

    def settle(self, ledger_id: str, actual: Decimal | None, error: bool = False) -> None:
        with self.sessions.begin() as db:
            item = db.get(Ledger, ledger_id)
            if item is None:
                raise ValueError("Réservation introuvable")
            if actual is not None:
                # Sur-facturation par rapport à la prévision possible : voir README.
                item.amount_eur = float(actual)
            item.state = "unknown" if error else "settled"

    def token_estimate(self, prompt: str, provider: str, max_output: int) -> Decimal:
        input_price, output_price = self.settings.prices[provider]
        # Prévision prudente mais NON garantie (tokenisation, raisonnement, cache variables).
        estimated_input = len(prompt.encode("utf-8")) // 2 + 200
        return (Decimal(estimated_input) * input_price + Decimal(max_output) * output_price) / Decimal(1_000_000)

    def actual_cost(self, provider: str, input_tokens: int, output_tokens: int) -> Decimal:
        p_in, p_out = self.settings.prices[provider]
        return (Decimal(input_tokens) * p_in + Decimal(output_tokens) * p_out) / Decimal(1_000_000)
