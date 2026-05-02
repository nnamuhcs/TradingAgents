"""Database models + connection helpers (Postgres via SQLAlchemy async)."""
from __future__ import annotations

import os
from datetime import datetime
from typing import AsyncIterator, Optional

from sqlalchemy import JSON, Column, DateTime, Integer, String, Text, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://tradingagents:tradingagents@postgres:5432/tradingagents",
)

Base = declarative_base()


class Run(Base):
    __tablename__ = "runs"

    id = Column(String(36), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="pending", nullable=False, index=True)
    # pending / running / completed / failed / cancelled

    ticker_source = Column(String(20), nullable=False)  # manual / scan-N
    symbols = Column(JSON, nullable=False)               # list of strings
    analysis_date = Column(String(10), nullable=False)
    analysts = Column(JSON, nullable=False)              # list of analyst types
    research_depth = Column(Integer, default=1)
    risk_rounds = Column(Integer, default=1)
    language = Column(String(40), default="English")

    llm_provider = Column(String(40), nullable=False)
    deep_model = Column(String(80), nullable=False)
    quick_model = Column(String(80), nullable=False)
    anthropic_effort = Column(String(20), nullable=True)
    openai_reasoning_effort = Column(String(20), nullable=True)
    google_thinking_level = Column(String(20), nullable=True)

    decisions = Column(JSON, default=dict)               # {symbol: decision_text}
    reports = Column(JSON, default=dict)                  # {symbol: {section: markdown}}
    error = Column(Text, nullable=True)


_engine = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine():
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(DATABASE_URL, echo=False, future=True, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    return _sessionmaker  # type: ignore[return-value]


async def init_db() -> None:
    """Create tables if they don't exist; add new columns if the schema
    has evolved since the original CREATE."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Lightweight ALTERs for added columns. Idempotent.
        await conn.exec_driver_sql(
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS reports JSONB DEFAULT '{}'::jsonb"
        )


async def session_scope() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    async with sm() as session:
        yield session
