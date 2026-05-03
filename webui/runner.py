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

    def _on_progress(event: dict[str, Any]) -> None:
        bus.publish(run_id, "scanner_layer", event)

    scanner = MarketScanner(
        provider=snap["llm_provider"],
        model=snap["deep_model"],
        progress_callback=_on_progress,
    )
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


def _extract_rating(decision_text: str) -> str:
    """Heuristic-extract a Buy/Overweight/Hold/Underweight/Sell rating from
    the Portfolio Manager's free-text decision."""
    if not decision_text:
        return "Hold"
    t = decision_text.strip()
    upper = t.upper()
    # Look for explicit FINAL RECOMMENDATION line first
    for label in ("STRONG BUY", "OVERWEIGHT", "BUY", "ACCUMULATE",
                  "HOLD", "NEUTRAL",
                  "UNDERWEIGHT", "REDUCE", "SELL", "STRONG SELL"):
        if label in upper:
            mapping = {
                "STRONG BUY": "Buy",
                "OVERWEIGHT": "Overweight",
                "BUY": "Buy",
                "ACCUMULATE": "Buy",
                "HOLD": "Hold",
                "NEUTRAL": "Hold",
                "UNDERWEIGHT": "Underweight",
                "REDUCE": "Underweight",
                "SELL": "Sell",
                "STRONG SELL": "Sell",
            }
            return mapping[label]
    return "Hold"


def _verve_publish_factory(run_id: str, symbol: str):
    """Wrap bus.publish to also emit Verve-compatible event aliases.

    The legacy frontend (cli/static/style.css era) and the new Verve drop-in
    expect slightly different field shapes. We emit BOTH so neither breaks:

      • agent_status: legacy uses long agent names ("Market Analyst") and
        statuses pending/in_progress/completed/skipped. Verve expects short
        keys ("market", "bull", "trader", "risk", "portfolio") and statuses
        wait/live/done. We always publish both shapes.
      • message: legacy uses {type, content}. Verve uses {agent, text, round?}.
        We add an alias when we can infer agent from context (Bull/Bear).
      • tool_call: legacy uses {tool}. Verve uses {name}. We populate both.
      • decision: legacy fires inside symbol_done. Verve listens for an
        explicit 'decision' event. The runner emits one explicitly.
    """
    AGENT_LONG_TO_SHORT = {
        "Market Analyst":       "market",
        "Social Analyst":       "social",
        "News Analyst":         "news",
        "Fundamentals Analyst": "fundamentals",
        "Bull Researcher":      "bull",
        "Bear Researcher":      "bear",
        "Research Manager":     "research",  # not in Verve roster but harmless
        "Trader":               "trader",
        "Aggressive Analyst":   "risk",      # collapse the 3 risk seats to 'risk'
        "Neutral Analyst":      "risk",
        "Conservative Analyst": "risk",
        "Portfolio Manager":    "portfolio",
    }
    STATUS_TO_VERVE = {
        "pending":    "wait",
        "skipped":    "wait",
        "in_progress":"live",
        "completed":  "done",
    }

    def publish(event: str, data: dict) -> None:
        # 1) Always send the legacy shape verbatim
        bus.publish(run_id, event, {"symbol": symbol, **data})

        # 2) Translate to Verve shape where it differs, and send under
        #    the same event name (additional emission, not replacement)
        if event == "agent_status":
            short = AGENT_LONG_TO_SHORT.get(data.get("agent", ""))
            v_status = STATUS_TO_VERVE.get(data.get("status", ""), data.get("status"))
            if short and v_status:
                bus.publish(run_id, "agent_status", {
                    "symbol": symbol, "agent": short, "status": v_status,
                })
        elif event == "agent_states":
            # Send a per-agent status burst in Verve shape so the Verve UI
            # can hydrate its initial agent panel.
            states = data.get("states", {})
            for long_name, status in states.items():
                short = AGENT_LONG_TO_SHORT.get(long_name)
                v_status = STATUS_TO_VERVE.get(status, status)
                if short and v_status:
                    bus.publish(run_id, "agent_status", {
                        "symbol": symbol, "agent": short, "status": v_status,
                    })
        elif event == "tool_call":
            # alias 'tool' → 'name'
            if "tool" in data and "name" not in data:
                bus.publish(run_id, "tool_call", {
                    "symbol": symbol,
                    "name": data["tool"],
                    "args": data.get("args"),
                })
    return publish


def _propagate(run_id: str, symbol: str, snap: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Pure sync function — invoked inside a thread executor. No DB access.

    Streams the LangGraph chunks and emits TUI-identical agent_status /
    message / tool_call / report_section events via the EventBus. Returns
    (decision_text, reports_dict) so the caller can persist them to Postgres.
    """
    from webui.graph_stream import GraphStreamer

    config = _build_config(snap)
    bus.publish(run_id, "log", {"line": f"[{symbol}] Building TradingAgentsGraph"})

    selected_analysts = snap.get("analysts") or ["market", "social", "news", "fundamentals"]
    ta = TradingAgentsGraph(
        selected_analysts=selected_analysts,
        debug=False,
        config=config,
    )
    bus.publish(run_id, "log", {"line": f"[{symbol}] Streaming analysis for {snap['analysis_date']}"})

    publish = _verve_publish_factory(run_id, symbol)

    streamer = GraphStreamer(
        publish=publish,
        selected_analysts=selected_analysts,
    )
    streamer.emit_initial(symbol, snap["analysis_date"])

    init_state = ta.propagator.create_initial_state(symbol, snap["analysis_date"])
    args = ta.propagator.get_graph_args()

    final_state: dict[str, Any] = {}
    for chunk in ta.graph.stream(init_state, **args):
        streamer.emit_chunk(chunk)
        final_state = chunk

    streamer.emit_done()
    decision = ta.process_signal(final_state.get("final_trade_decision", "")) if final_state else ""
    return str(decision), streamer.reports


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
        reports: dict[str, dict[str, str]] = {}
        for symbol in symbols:
            bus.publish(run_id, "symbol_start", {"symbol": symbol})
            try:
                decision, sym_reports = await loop.run_in_executor(
                    None, functools.partial(_propagate, run_id, symbol, snap)
                )
                decisions[symbol] = decision
                reports[symbol] = sym_reports
                # Verve-shape: dedicated `decision` event with rating
                rating = _extract_rating(decision)
                bus.publish(run_id, "decision",
                            {"symbol": symbol, "rating": rating,
                             "decision_text": decision})
                bus.publish(run_id, "symbol_done",
                            {"symbol": symbol, "decision": decision,
                             "reports_saved": list(sym_reports.keys())})
                # Persist incremental progress (decisions + reports)
                await _set_status(run_id, decisions=dict(decisions), reports=dict(reports))
            except Exception as e:  # pragma: no cover
                bus.publish(run_id, "symbol_error", {"symbol": symbol, "error": str(e)})
                decisions[symbol] = f"ERROR: {e}"
                await _set_status(run_id, decisions=dict(decisions), reports=dict(reports))

        # All symbols done
        bus.publish(run_id, "run_done", {})

        # 3. Mark complete
        await _set_status(
            run_id,
            decisions=decisions,
            reports=reports,
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
