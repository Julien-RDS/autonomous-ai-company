"""Réservation AVANT appel, écriture d'usage APRÈS appel (contrôle approximatif, non plafond opérateur)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text

from .database import Ledger, AuditEvent
from .money import money
import json


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

    def reserve(self, run_id: str, provider: str, estimate: Decimal, details: str = "", *, operation_key: str | None = None, currency: str = "EUR") -> str:
        if not estimate.is_finite() or estimate <= 0:
            raise ValueError("Estimation finie strictement positive requise")
        estimate = money(estimate, currency)
        if estimate == 0:
            raise ValueError("Estimation sous la précision monétaire")
        # Serialize check-and-insert across processes sharing the same database.
        with self.sessions.begin() as db:
            dialect = db.get_bind().dialect.name
            if dialect == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            elif dialect == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(7109241)"))
            else:
                raise RuntimeError("Réservations supportées sur SQLite et PostgreSQL uniquement")
            if operation_key and db.scalar(select(Ledger.id).where(Ledger.operation_key == operation_key)):
                raise RuntimeError("Opération déjà réservée; rapprochement requis avant nouvel appel")
            now = datetime.now(timezone.utc)
            start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
            total = select(func.coalesce(func.sum(Ledger.amount_eur), 0))
            monthly = Decimal(str(db.scalar(total.where(Ledger.created_at >= start)) or 0))
            one_run = Decimal(str(db.scalar(total.where(Ledger.run_id == run_id)) or 0))
            if monthly + estimate > self.settings.monthly_limit:
                raise BudgetExceeded(f"Plafond mensuel local atteint ({monthly:.4f} € comptabilisés)")
            if one_run + estimate > self.settings.run_limit:
                raise BudgetExceeded(f"Plafond de session atteint ({one_run:.4f} € comptabilisés)")
            record = Ledger(run_id=run_id, provider=provider, amount_eur=estimate, estimated_eur=estimate, currency=currency,
                            operation_key=operation_key,
                            tariff_version=json.dumps([str(p) for p in self.settings.prices.get(provider, ())]),
                            details=details[:250])
            db.add(record)
            db.flush()
            return record.id

    def settle(self, ledger_id: str, actual: Decimal | None, error: bool = False) -> None:
        if actual is not None and (not actual.is_finite() or actual < 0):
            raise ValueError("Coût réel fini positif ou nul requis")
        with self.sessions.begin() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            else:
                db.execute(text("SELECT pg_advisory_xact_lock(7109241)"))
            item = db.scalar(select(Ledger).where(Ledger.id == ledger_id).with_for_update())
            if item is None:
                raise ValueError("Réservation introuvable")
            if item.state != "reserved":
                raise ValueError("Réservation déjà régularisée")
            if actual is not None:
                # Sur-facturation par rapport à la prévision possible : voir README.
                item.amount_eur = max(item.amount_eur, money(actual)) if error else money(actual)
                item.actual_eur = money(actual)
            item.state = "unknown" if error else "settled"

    def token_estimate(self, prompt: str, provider: str, max_output: int) -> Decimal:
        input_price, output_price = self.settings.prices[provider]
        # Prévision prudente mais NON garantie (tokenisation, raisonnement, cache variables).
        estimated_input = len(prompt.encode("utf-8")) // 2 + 200
        return money((Decimal(estimated_input) * input_price + Decimal(max_output) * output_price) / Decimal(1_000_000))

    def actual_cost(self, provider: str, input_tokens: int, output_tokens: int) -> Decimal:
        p_in, p_out = self.settings.prices[provider]
        return money((Decimal(input_tokens) * p_in + Decimal(output_tokens) * p_out) / Decimal(1_000_000))

    def reconcile_abandoned(self, before: datetime) -> int:
        """Keep all possibly spent money; never retry or release an uncertain charge."""
        from sqlalchemy import update
        with self.sessions.begin() as db:
            result = db.execute(update(Ledger).where(
                Ledger.state == "reserved", Ledger.created_at < before
            ).values(state="unknown"))
            return result.rowcount

    def reconcile_unknown(self, ledger_id: str, amount: Decimal, reference: str, currency='EUR') -> None:
        """Explicit human-confirmed charge. Never authorizes replay of the operation."""
        amount = money(amount, currency)
        if not reference.strip() or len(reference) > 500:
            raise ValueError('Référence de rapprochement requise (500 caractères maximum)')
        with self.sessions.begin() as db:
            if db.get_bind().dialect.name == 'sqlite':
                db.execute(text('BEGIN IMMEDIATE'))
            else:
                db.execute(text('SELECT pg_advisory_xact_lock(7109241)'))
            item = db.scalar(select(Ledger).where(Ledger.id == ledger_id).with_for_update())
            if item is None:
                raise ValueError('Écriture inconnue')
            if item.state == 'reconciled' and item.amount_eur == amount and item.reconciliation_reference == reference:
                return
            if item.state != 'unknown':
                raise ValueError('Seuls les coûts inconnus peuvent être rapprochés')
            item.amount_eur = amount
            item.actual_eur = amount
            item.state = 'reconciled'
            item.reconciliation_reference = reference
            db.add(AuditEvent(entity_id=item.id, kind='human_cost_reconciled'))
