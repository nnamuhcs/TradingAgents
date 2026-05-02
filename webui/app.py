"""TradingAgents WebUI — FastAPI app.

Run locally:
    pip install -e '.[webui]'
    DATABASE_URL=postgresql+asyncpg://... uvicorn webui.app:app --host 0.0.0.0 --port 8000

In K8s:
    See k8s/webui-deployment.yaml + k8s/postgres.yaml
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from tradingagents.scanner import MarketScanner

from webui.charts import get_ohlcv
from webui.db import Run, get_sessionmaker, init_db
from webui.events import bus
from webui.runner import kick_off


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("webui")


app = FastAPI(title="TradingAgents WebUI", version="0.1.0")

# Permissive CORS for the optional Ingress/dev workflow.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    logger.info("DB initialized")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ──────────────────────────────────────────────
# Run management
# ──────────────────────────────────────────────


class StartRunRequest(BaseModel):
    ticker_source: str = Field("manual", description="manual / scan-5 / scan-10 / scan-20")
    symbols: list[str] = Field(default_factory=list, description="Required when ticker_source=manual; can be multiple")
    analysis_date: str = Field(..., description="YYYY-MM-DD")
    analysts: list[str] = Field(default_factory=lambda: ["market", "social", "news", "fundamentals"])
    research_depth: int = 1
    risk_rounds: int = 1
    language: str = "English"

    llm_provider: str = Field(default_factory=lambda: os.getenv("LLM_PROVIDER", "github-copilot"))
    deep_model: str = Field(default_factory=lambda: os.getenv("DEEP_THINK_LLM", "claude-opus-4.7"))
    quick_model: str = Field(default_factory=lambda: os.getenv("QUICK_THINK_LLM", "claude-opus-4.7"))
    anthropic_effort: str | None = None
    openai_reasoning_effort: str | None = None
    google_thinking_level: str | None = None


@app.post("/api/runs")
async def start_run(req: StartRunRequest) -> dict[str, str]:
    if req.ticker_source == "manual" and not req.symbols:
        raise HTTPException(400, "symbols is required when ticker_source=manual")
    if req.ticker_source not in {"manual", "scan-5", "scan-10", "scan-20"}:
        raise HTTPException(400, f"invalid ticker_source: {req.ticker_source}")

    run_id = str(uuid.uuid4())
    sm = get_sessionmaker()
    async with sm() as session:
        run = Run(
            id=run_id,
            status="pending",
            ticker_source=req.ticker_source,
            symbols=[s.strip().upper() for s in req.symbols],
            analysis_date=req.analysis_date,
            analysts=req.analysts,
            research_depth=req.research_depth,
            risk_rounds=req.risk_rounds,
            language=req.language,
            llm_provider=req.llm_provider,
            deep_model=req.deep_model,
            quick_model=req.quick_model,
            anthropic_effort=req.anthropic_effort,
            openai_reasoning_effort=req.openai_reasoning_effort,
            google_thinking_level=req.google_thinking_level,
            decisions={},
        )
        session.add(run)
        await session.commit()

    bus.open(run_id)
    kick_off(run_id)
    return {"run_id": run_id}


@app.get("/api/runs")
async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                select(Run).order_by(desc(Run.created_at)).limit(limit)
            )
        ).scalars().all()
    return [_run_to_dict(r) for r in rows]


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    sm = get_sessionmaker()
    async with sm() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    return _run_to_dict(run)


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    if not bus.has(run_id):
        raise HTTPException(404, "no live event stream for this run (it may have finished)")

    async def gen():
        async for chunk in bus.subscribe(run_id):
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _run_to_dict(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "status": r.status,
        "ticker_source": r.ticker_source,
        "symbols": r.symbols,
        "analysis_date": r.analysis_date,
        "analysts": r.analysts,
        "research_depth": r.research_depth,
        "risk_rounds": r.risk_rounds,
        "language": r.language,
        "llm_provider": r.llm_provider,
        "deep_model": r.deep_model,
        "quick_model": r.quick_model,
        "decisions": r.decisions,
        "error": r.error,
    }


# ──────────────────────────────────────────────
# Scanner & charts
# ──────────────────────────────────────────────


@app.get("/api/scan")
async def api_scan(n: int = 10) -> dict[str, Any]:
    n = max(1, min(n, 20))
    provider = os.getenv("LLM_PROVIDER", "github-copilot")
    model = os.getenv("SCANNER_LLM") or os.getenv("DEEP_THINK_LLM", "claude-opus-4.7")
    scanner = MarketScanner(provider=provider, model=model)
    import asyncio
    result = await asyncio.get_event_loop().run_in_executor(None, scanner.scan)
    detailed = result.get("detailed", {})
    return {
        "picks": (detailed.get("picks") or [])[:n],
        "market_regime": detailed.get("market_regime"),
        "themes": detailed.get("themes"),
        "candidates": detailed.get("candidates", []),
    }


@app.get("/api/chart/{symbol}")
async def api_chart(symbol: str, period: str = "6mo") -> dict[str, Any]:
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: get_ohlcv(symbol.upper(), period)
    )
