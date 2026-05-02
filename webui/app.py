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
import time
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
from webui.movers import get_movers
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
    # Kick off Yahoo Live WebSocket with the default anchor set; the SSE
    # stream will dynamically extend the subscription with whatever ends
    # up in the marquee feed.
    try:
        from webui.movers import ANCHORS, get_movers
        from webui.yahoo_live import ticker
        await ticker.start([s for s in ANCHORS if s])
        logger.info("Yahoo Live WebSocket started")
        # Warm the movers cache so the first SSE yield doesn't block 30s on
        # yfinance screening of the S&P 500 universe.
        import asyncio
        asyncio.create_task(asyncio.get_event_loop().run_in_executor(
            None, lambda: get_movers(n_gainers=8, n_losers=8)
        ))
        logger.info("Movers cache warm-up scheduled")
    except Exception as e:  # pragma: no cover
        logger.warning("Yahoo Live WS / movers warm-up not started: %s", e)


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


@app.get("/api/movers/stream")
async def api_movers_stream(
    pinned: str = "",
    n_gainers: int = 8,
    n_losers: int = 8,
) -> StreamingResponse:
    """Server-Sent Events stream of marquee updates.

    Sends a fresh snapshot every ~5s while a Yahoo WebSocket feed is alive,
    or every ~60s falling back to /api/movers' yfinance computation.
    """
    import asyncio
    import json
    from webui.yahoo_live import ticker

    pins = [p.strip().upper() for p in pinned.split(",") if p.strip()]

    async def gen():
        from webui.movers import get_movers, _recent_run_symbols, ANCHORS
        from webui.yahoo_live import ticker

        first = True
        while True:
            # Fast initial snapshot: live WS prices for ANCHORS + pins + recent
            # (no yfinance round-trip). Yields immediately so the browser sees
            # something in <1s even on a cold cache.
            if first:
                first = False
                snap = ticker.get_snapshot()
                anchor_keys = [s.replace("^", "") for s in ANCHORS]
                quick_feed = []
                for s in anchor_keys + [p.upper() for p in pins]:
                    live = snap.get(s) or snap.get(f"^{s}")
                    if live and live.get("p") is not None and live.get("c") is not None:
                        quick_feed.append({
                            "s": s, "p": live["p"], "c": live["c"],
                            "kind": "anchor" if s in anchor_keys else "pinned",
                            "live": True,
                        })
                if quick_feed:
                    yield (
                        "event: snapshot\n"
                        f"data: {json.dumps({'feed': quick_feed, 'live': ticker.is_live, 'ts': int(time.time())}, default=str)}\n\n"
                    )

            # Full pass: anchors + pinned + recent + top movers (slow, cached)
            recent = await _recent_run_symbols(days=30, limit=50)
            extras_for_universe = list(set(pins) | set(recent))
            feed = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: get_movers(
                    n_gainers=n_gainers,
                    n_losers=n_losers,
                    extra=extras_for_universe,
                    pinned=list(pins) + [s for s in recent if s not in pins],
                ),
            )

            symbols_in_feed = [item.get("s", "") for item in feed]
            await ticker.update_subscriptions([s.upper() for s in symbols_in_feed if s])

            snapshot = ticker.get_snapshot()
            for item in feed:
                live = snapshot.get(item["s"]) or snapshot.get(item["s"].upper())
                if live and live.get("p") is not None and live.get("c") is not None:
                    item["p"] = live["p"]
                    item["c"] = live["c"]
                    item["live"] = True

            payload = {"feed": feed, "live": ticker.is_live, "ts": int(time.time())}
            yield f"event: snapshot\ndata: {json.dumps(payload, default=str)}\n\n"

            await asyncio.sleep(5 if ticker.is_live else 60)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: get_ohlcv(symbol.upper(), period)
    )


@app.get("/api/movers")
async def api_movers(
    n_gainers: int = 8,
    n_losers: int = 8,
    extra: str = "",
    pinned: str = "",
    include_recent_runs: bool = True,
) -> dict[str, Any]:
    """Top market movers (gainers + losers) interleaved with anchor indices,
    user-pinned tickers, and symbols from recent analyses.

    Backed by yfinance and a server-side ~55s cache.
      • `pinned=AAA,BBB` — comma-separated user pins (kind=pinned in feed)
      • `include_recent_runs=true` — auto-include symbols from runs in the
        last 30 days (kind=recent in feed)
      • `extra=` — additional universe inclusions (no special tagging)
    """
    import asyncio
    extras = [e.strip().upper() for e in extra.split(",") if e.strip()]
    pins = [p.strip().upper() for p in pinned.split(",") if p.strip()]

    recent: list[str] = []
    if include_recent_runs:
        from webui.movers import _recent_run_symbols
        recent = await _recent_run_symbols(days=30, limit=50)

    # Recent runs go into the universe so movers can pick them up; also tag
    # explicitly via `pinned` slot when not already covered.
    extras_for_universe = list(set(extras) | set(recent))

    feed = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: get_movers(
            n_gainers=n_gainers,
            n_losers=n_losers,
            extra=extras_for_universe,
            pinned=list(pins) + [s for s in recent if s not in pins],
        ),
    )

    # Mark the recent-only entries (those that ended up in the pinned slot
    # but weren't user-pinned) as kind=recent for UI styling.
    pin_set = set(pins)
    recent_set = set(recent)
    for item in feed:
        if item.get("kind") == "pinned":
            sym = (item.get("s") or "").upper()
            if sym not in pin_set and sym in recent_set:
                item["kind"] = "recent"

    return {"feed": feed, "count": len(feed), "pinned": pins, "recent": recent}


@app.get("/api/chart/{symbol}")
async def api_chart(symbol: str, period: str = "6mo") -> dict[str, Any]:
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: get_ohlcv(symbol.upper(), period)
    )
