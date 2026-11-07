"""Modèle SQLAlchemy utilisable avec SQLite (démo) ou PostgreSQL (service)."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from .money import Money
from pathlib import Path
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(25), default="running")
    mode: Mapped[str] = mapped_column(String(10))
    mission: Mapped[str] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    selection_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    messages: Mapped[list["Message"]] = relationship(back_populates="run")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(default=True)
    run: Mapped[Run] = relationship(back_populates="messages")


class Ledger(Base):
    __tablename__ = "ledger"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(20))
    amount_eur: Mapped[Decimal] = mapped_column(Money())
    estimated_eur: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    actual_eur: Mapped[Decimal | None] = mapped_column(Money(), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", server_default="EUR")
    operation_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    tariff_version: Mapped[str] = mapped_column(Text, default="legacy", server_default="legacy")
    state: Mapped[str] = mapped_column(String(20), default="reserved")
    details: Mapped[str] = mapped_column(String(250), default="")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(30), default="validation")
    status: Mapped[str] = mapped_column(String(30), default="pending_review")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    success: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_eur: Mapped[Decimal] = mapped_column(Money(), default=Decimal(0))
    notes: Mapped[str] = mapped_column(Text, default="")



class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="active")
    __table_args__ = (UniqueConstraint("provider", "model"),)


class WorkItem(Base):
    """Execution queue, distinct from human-facing prototype/validation tickets."""
    __tablename__ = "work_items"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    token: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    external: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (
        CheckConstraint("state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','RETRY_SCHEDULED','CANCELLED','WAITING_APPROVAL')", name="ck_work_state"),
        CheckConstraint("max_attempts > 0 AND attempts >= 0", name="ck_work_attempts"),
    )


class Attempt(Base):
    __tablename__ = "task_attempts"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("work_items.id"), index=True)
    owner: Mapped[str] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String(30), default="RUNNING")


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), unique=True)
    actor: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=new_id)
    entity_id: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

def session_factory(db_url: str):
    if db_url.startswith("sqlite:///"):
        path = Path(db_url.split("sqlite:///", 1)[1])
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(db_url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, record):
            connection.execute("PRAGMA foreign_keys=ON")
    from alembic import command
    from alembic.config import Config
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    with engine.begin() as connection:
        if engine.dialect.name == 'postgresql':
            from sqlalchemy import text
            connection.execute(text('SELECT pg_advisory_xact_lock(7109243)'))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return sessionmaker(bind=engine, expire_on_commit=False)
