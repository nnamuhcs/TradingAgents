"""Background runner: executes TradingAgentsGraph for one or more symbols
and publishes events to the EventBus.

Design:
  • All DB I/O happens on FastAPI's main asyncio loop (asyncpg can't cross loops).
  • CPU/blocking trading-graph work runs in a thread executor with plain Python
    primitives — no DB or async work inside the executor.
"""
from __future__ import annotations

import asyncio
import functools
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.scanner import MarketScanner

from webui.db import Run, get_sessionmaker
from webui.events import bus


def _build_config(snap: dict[str, Any]) -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    config["llm_provider"] = snap["llm_provider"]
    config["deep_think_llm"] = snap["deep_model"]
    config["quick_think_llm"] = snap["quick_model"]
    config["max_debate_rounds"] = snap["research_depth"]
    config["max_risk_discuss_rounds"] = snap["risk_rounds"]
    config["output_language"] = snap["language"]

    data_vendor = os.getenv("DATA_VENDOR", "yfinance")
    config["data_vendors"] = {
        "core_stock_apis": data_vendor,
        "technical_indicators": data_vendor,
        "fundamental_data": data_vendor,
        "news_data": data_vendor,
    }

    if snap.get("anthropic_effort"):
        config["anthropic_effort"] = snap["anthropic_effort"]
    if snap.get("openai_reasoning_effort"):
        config["reasoning_effort"] = snap["openai_reasoning_effort"]
    if snap.get("google_thinking_level"):
        config["thinking_level"] = snap["google_thinking_level"]
    return config


def _scanner_resolve(run_id: str, snap: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Pure sync function — invoked inside a thread executor. No DB access here."""
    if snap["ticker_source"] == "manual":
        return list(snap["symbols"]), {}

    n = int(snap["ticker_source"].split("-")[1])
    bus.publish(run_id, "log", {"line": f"Running Market Scanner (top {n})..."})
    scanner = MarketScanner(provider=snap["llm_provider"], model=snap["deep_model"])
    result = scanner.scan()
    detailed = result.get("detailed", {})
    picks = (detailed.get("picks") or [])[:n]
    bus.publish(
        run_id,
        "scanner_picks",
        {
            "picks": picks,
            "market_regime": detailed.get("market_regime"),
            "themes": detailed.get("themes"),
        },
    )
    symbols = [p["symbol"] for p in picks]
    bus.publish(run_id, "log", {"line": f"Scanner picked: {', '.join(symbols)}"})
    return symbols, {"picks": picks, "market_regime": detailed.get("market_regime"),
                     "themes": detailed.get("themes")}


def _propagate(run_id: str, symbol: str, snap: dict[str, Any]) -> str:
    """Pure sync function — invoked inside a thread executor. No DB access."""
    config = _build_config(snap)
    bus.publish(run_id, "log", {"line": f"[{symbol}] Building TradingAgentsGraph"})

    selected_analysts = snap.get("analysts") or ["market", "social", "news", "fundamentals"]
    ta = TradingAgentsGraph(
        selected_analysts=selected_analysts,
        debug=False,
        config=config,
    )
    bus.publish(run_id, "log", {"line": f"[{symbol}] Propagating analysis for {snap['analysis_date']}"})
    _, decision = ta.propagate(symbol, snap["analysis_date"])
    return str(decision)


def _snapshot(run: Run) -> dict[str, Any]:
    """Convert a SQLAlchemy Run into a plain dict so we can pass it across threads."""
    return {
        "ticker_source": run.ticker_source,
        "symbols": list(run.symbols or []),
        "analysis_date": run.analysis_date,
        "analysts": list(run.analysts or []),
        "research_depth": run.research_depth or 1,
        "risk_rounds": run.risk_rounds or 1,
        "language": run.language or "English",
        "llm_provider": run.llm_provider,
        "deep_model": run.deep_model,
        "quick_model": run.quick_model,
        "anthropic_effort": run.anthropic_effort,
        "openai_reasoning_effort": run.openai_reasoning_effort,
        "google_thinking_level": run.google_thinking_level,
    }


async def _load_run(run_id: str) -> Run:
    sm = get_sessionmaker()
    async with sm() as session:
        return (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()


async def _set_status(run_id: str, **fields: Any) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        for k, v in fields.items():
            setattr(run, k, v)
        await session.commit()


async def _run_async(run_id: str) -> None:
    try:
        await _set_status(run_id, status="running")
        bus.publish(run_id, "log", {"line": f"Run {run_id} starting"})

        # Load snapshot on the main async loop
        run = await _load_run(run_id)
        snap = _snapshot(run)

        loop = asyncio.get_running_loop()

        # 1. Resolve symbols (may invoke scanner in a thread executor)
        symbols, scanner_meta = await loop.run_in_executor(
            None, functools.partial(_scanner_resolve, run_id, snap)
        )
        if not symbols:
            raise RuntimeError("No symbols to analyze")

        # Persist resolved symbols back to DB
        await _set_status(run_id, symbols=symbols)
        snap["symbols"] = symbols

        # 2. For each symbol, run the trading graph in the executor
        decisions: dict[str, str] = {}
        for symbol in symbols:
            bus.publish(run_id, "symbol_start", {"symbol": symbol})
            try:
                decision = await loop.run_in_executor(
                    None, functools.partial(_propagate, run_id, symbol, snap)
                )
                decisions[symbol] = decision
                bus.publish(run_id, "symbol_done", {"symbol": symbol, "decision": decision})
                # Persist incremental progress
                await _set_status(run_id, decisions=dict(decisions))
            except Exception as e:  # pragma: no cover
                bus.publish(run_id, "symbol_error", {"symbol": symbol, "error": str(e)})
                decisions[symbol] = f"ERROR: {e}"
                await _set_status(run_id, decisions=dict(decisions))

        # 3. Mark complete
        await _set_status(
            run_id,
            decisions=decisions,
            status="completed",
            finished_at=datetime.utcnow(),
        )
        bus.publish(run_id, "final_decision", {"decisions": decisions})
    except Exception as e:  # pragma: no cover
        await _set_status(
            run_id, status="failed", error=str(e), finished_at=datetime.utcnow()
        )
        bus.publish(run_id, "error", {"message": str(e)})
    finally:
        bus.close(run_id)


def kick_off(run_id: str) -> asyncio.Task:
    return asyncio.create_task(_run_async(run_id))
