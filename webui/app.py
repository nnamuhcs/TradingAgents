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

    # 1. Clean up zombie runs from a previous pod that didn't shut down cleanly
    await _reap_stale_runs(max_age_minutes=30)
    # 2. Schedule periodic reaping while we run
    import asyncio
    asyncio.create_task(_zombie_reaper_loop())

    # 3. Yahoo Live WebSocket + warm movers cache
    try:
        from webui.movers import ANCHORS, get_movers
        from webui.yahoo_live import ticker
        await ticker.start([s for s in ANCHORS if s])
        logger.info("Yahoo Live WebSocket started")
        asyncio.create_task(asyncio.get_event_loop().run_in_executor(
            None, lambda: get_movers(n_gainers=8, n_losers=8)
        ))
        logger.info("Movers cache warm-up scheduled")
    except Exception as e:  # pragma: no cover
        logger.warning("Yahoo Live WS / movers warm-up not started: %s", e)


async def _reap_stale_runs(max_age_minutes: int = 30) -> None:
    """Mark any 'running' runs older than `max_age_minutes` as 'failed'.

    These are runs whose worker died (pod restart, OOM, timeout) and never
    got to emit a final status. Without this, they show "Running" forever
    in the History page.
    """
    from datetime import timedelta
    from sqlalchemy import update
    from webui.db import Run, get_sessionmaker

    cutoff = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    sm = get_sessionmaker()
    try:
        async with sm() as session:
            result = await session.execute(
                update(Run)
                .where(Run.status.in_(["running", "pending"]))
                .where(Run.created_at < cutoff)
                .values(status="failed", error="Worker died (pod restart or timeout)",
                        finished_at=datetime.utcnow())
            )
            await session.commit()
            if result.rowcount:
                logger.info("Reaped %d stale runs", result.rowcount)
    except Exception as e:  # pragma: no cover
        logger.warning("zombie reaper failed: %s", e)


async def _zombie_reaper_loop() -> None:
    import asyncio
    while True:
        await asyncio.sleep(300)  # every 5 minutes
        await _reap_stale_runs(max_age_minutes=30)


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
    ticker_source: str = Field("manual", description="manual / scan-3 / scan-5 / scan-10 / scan-20")
    symbols: list[str] = Field(default_factory=list, description="Required when ticker_source=manual; can be multiple")
    analysis_date: str = Field(..., description="YYYY-MM-DD")
    analysts: list[str] = Field(default_factory=lambda: ["market", "social", "news", "fundamentals"])
    research_depth: int = 1
    risk_rounds: int = 1
    # Verve front-end aliases — accepted as fallback if research_depth/risk_rounds aren't set.
    max_debate_rounds: int | None = None
    max_risk_discuss_rounds: int | None = None
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
    if req.ticker_source not in {"manual", "scan-3", "scan-5", "scan-10", "scan-20"}:
        raise HTTPException(400, f"invalid ticker_source: {req.ticker_source}")

    # Cap manual symbols to 5 — multi-symbol runs are sequential and the
    # cumulative latency / token cost grows quickly past that.
    symbols = [s.strip().upper() for s in req.symbols][:5]
    if req.ticker_source == "manual" and len(req.symbols) > 5:
        # silently truncate but tell the client via an info log so the UI
        # can surface it
        pass

    # Honor Verve-style aliases when the new front-end posts them
    research_depth = req.research_depth
    risk_rounds = req.risk_rounds
    if req.max_debate_rounds is not None:
        research_depth = int(req.max_debate_rounds)
    if req.max_risk_discuss_rounds is not None:
        risk_rounds = int(req.max_risk_discuss_rounds)
    elif req.max_debate_rounds is not None:
        # If only debate provided, mirror it to risk too (matches TUI default).
        risk_rounds = int(req.max_debate_rounds)

    run_id = str(uuid.uuid4())
    sm = get_sessionmaker()
    async with sm() as session:
        run = Run(
            id=run_id,
            status="pending",
            ticker_source=req.ticker_source,
            symbols=symbols,
            analysis_date=req.analysis_date,
            analysts=req.analysts,
            research_depth=research_depth,
            risk_rounds=risk_rounds,
            language=req.language,
            llm_provider=req.llm_provider,
            deep_model=req.deep_model,
            quick_model=req.quick_model,
            anthropic_effort=req.anthropic_effort,
            openai_reasoning_effort=req.openai_reasoning_effort,
            google_thinking_level=req.google_thinking_level,
            decisions={},
            reports={},
        )
        session.add(run)
        await session.commit()

    bus.open(run_id)
    kick_off(run_id)
    return {"run_id": run_id}


@app.get("/api/runs")
async def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    # Reap stale runs before listing so the History page is honest
    await _reap_stale_runs(max_age_minutes=30)
    sm = get_sessionmaker()
    async with sm() as session:
        rows = (
            await session.execute(
                select(Run).order_by(desc(Run.created_at)).limit(limit)
            )
        ).scalars().all()
    return [_run_to_dict(r) for r in rows]


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict[str, Any]:
    """Mark a run as cancelled. Doesn't actually interrupt the executor task,
    but stops the user seeing it as 'running' forever."""
    sm = get_sessionmaker()
    async with sm() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, "run not found")
        if run.status in ("completed", "failed", "cancelled"):
            return _run_to_dict(run)
        run.status = "cancelled"
        run.finished_at = datetime.utcnow()
        run.error = (run.error or "") + " (user cancelled)"
        await session.commit()
    bus.publish(run_id, "error", {"message": "Run cancelled by user"})
    bus.close(run_id)
    return _run_to_dict(run)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    sm = get_sessionmaker()
    async with sm() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    return _run_to_dict(run)


def _build_full_report(run: Run, symbol: str) -> str:
    """Combine the persisted report sections into one nicely formatted Markdown
    document. Used by the JSON and download endpoints."""
    reports = (run.reports or {}).get(symbol, {}) if run.reports else {}
    decision = (run.decisions or {}).get(symbol, "")

    section_titles = [
        ("market_report",          "I.   Market / Technical Analysis"),
        ("sentiment_report",       "II.  Social Sentiment Analysis"),
        ("news_report",            "III. News Analysis"),
        ("fundamentals_report",    "IV.  Fundamentals Analysis"),
        ("investment_plan",        "V.   Research Team Debate (Bull / Bear / Manager)"),
        ("trader_investment_plan", "VI.  Trader's Investment Plan"),
        ("final_trade_decision",   "VII. Risk Management & Portfolio Manager Decision"),
    ]

    parts: list[str] = []
    parts.append(f"# TradingAgents — Full Report")
    parts.append("")
    parts.append(f"**Symbol:** `{symbol}`  |  **Analysis date:** `{run.analysis_date}`")
    parts.append("")
    parts.append(f"- Run ID: `{run.id}`")
    parts.append(f"- Generated: `{(run.finished_at or run.created_at).isoformat() if run.created_at else 'n/a'}` UTC")
    parts.append(f"- Provider: `{run.llm_provider}`  |  Deep model: `{run.deep_model}`  |  Quick model: `{run.quick_model}`")
    parts.append(f"- Analysts: `{', '.join(run.analysts or [])}`")
    parts.append(f"- Research depth: `{run.research_depth}`  |  Risk rounds: `{run.risk_rounds}`")
    parts.append("")
    if decision:
        parts.append("## Final Decision")
        parts.append("")
        parts.append(decision if decision.lstrip().startswith("#") else f"> {decision}")
        parts.append("")
    parts.append("---")
    parts.append("")
    for key, title in section_titles:
        content = reports.get(key)
        if not content:
            continue
        parts.append(f"## {title}")
        parts.append("")
        parts.append(content.strip())
        parts.append("")
        parts.append("---")
        parts.append("")
    if not reports:
        parts.append("_(No section content was persisted for this run.)_")
    return "\n".join(parts)


@app.get("/api/runs/{run_id}/report")
async def get_run_report(run_id: str, symbol: str | None = None) -> dict[str, Any]:
    """Return the assembled full report as JSON.

    If `symbol` omitted, returns reports for every symbol in the run.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    syms = [symbol] if symbol else (run.symbols or [])
    if not syms:
        return {"run_id": run_id, "reports": []}
    reports = []
    for s in syms:
        if not s:
            continue
        reports.append({
            "symbol": s,
            "markdown": _build_full_report(run, s),
            "decision": (run.decisions or {}).get(s, ""),
            "sections": list(((run.reports or {}).get(s, {}) or {}).keys()),
        })
    return {"run_id": run_id, "reports": reports}


@app.get("/api/runs/{run_id}/report.md")
async def download_run_report(run_id: str, symbol: str) -> StreamingResponse:
    """Download a single symbol's full report as a .md file."""
    sm = get_sessionmaker()
    async with sm() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    md = _build_full_report(run, symbol.upper())

    async def gen():
        yield md.encode("utf-8")

    fname = f"{symbol.upper()}_{run.analysis_date}_{run_id[:8]}.md"
    return StreamingResponse(
        gen(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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
        "reports": r.reports,
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


@app.get("/api/scan/stream")
async def api_scan_stream(n: int = 10) -> StreamingResponse:
    """Server-Sent Events stream of scanner progress + final picks.

    Emits one `scanner_layer` event per layer transition (4 layers), then a
    final `picks` event with the picks/market_regime/themes payload.
    """
    n = max(1, min(n, 20))
    provider = os.getenv("LLM_PROVIDER", "github-copilot")
    model = os.getenv("SCANNER_LLM") or os.getenv("DEEP_THINK_LLM", "claude-opus-4.7")

    import asyncio
    import json as _json

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _on_progress(event: dict) -> None:
        # Called from a thread executor; bounce events back to the asyncio loop
        loop.call_soon_threadsafe(queue.put_nowait, ("scanner_layer", event))

    def _run_scan() -> dict:
        scanner = MarketScanner(provider=provider, model=model, progress_callback=_on_progress)
        return scanner.scan()

    async def producer():
        try:
            result = await loop.run_in_executor(None, _run_scan)
            detailed = result.get("detailed", {})
            await queue.put(("picks", {
                "picks": (detailed.get("picks") or [])[:n],
                "market_regime": detailed.get("market_regime"),
                "themes": detailed.get("themes"),
                "candidates": detailed.get("candidates", []),
                "all_scored": detailed.get("all_scored"),
            }))
        except Exception as e:  # pragma: no cover
            await queue.put(("error", {"message": str(e)}))
        finally:
            await queue.put(("done", {}))

    asyncio.create_task(producer())

    async def gen():
        while True:
            event, data = await queue.get()
            yield f"event: {event}\ndata: {_json.dumps(data, default=str)}\n\n"
            if event == "done":
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
