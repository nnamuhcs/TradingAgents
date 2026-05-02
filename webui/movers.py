"""Market movers: compute top gainers / losers / volume leaders from a
curated universe by fetching daily-resolution price data via yfinance.

This is a pull-based pseudo-stream: cache for ~60s, recompute on demand.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf
from sqlalchemy import select

from tradingagents.scanner.market_scanner import _get_sp500_tickers

logger = logging.getLogger(__name__)


# Always-pinned anchors (shown first in the ticker, never demoted)
ANCHORS: list[str] = [
    "SPY", "QQQ", "DIA", "IWM", "^VIX",       # US equity benchmarks + VIX
    "^IXIC", "^GSPC", "^DJI",                 # Spot indices
    "GLD", "SLV", "USO",                       # Commodities ETFs
    "TLT", "HYG",                              # Bonds
    "UUP", "EURUSD=X", "USDJPY=X",            # FX
    "BTC-USD", "ETH-USD",                      # Crypto
]

# Mega-cap bellwethers always considered for movers
BELLWETHERS: list[str] = [
    "NVDA", "AAPL", "MSFT", "GOOG", "META", "AMZN", "TSLA", "AVGO",
    "AMD", "BRK-B", "JPM", "V", "MA", "UNH", "XOM", "WMT", "LLY",
    "NFLX", "PLTR", "CRM", "ORCL", "COIN", "SMCI", "ARM",
]


_cache: dict[str, Any] = {"ts": 0.0, "data": []}
_CACHE_TTL_S = 55  # browser refreshes at 60s; keep server cache slightly shorter


def _build_universe(extra: list[str] | None = None) -> list[str]:
    universe: list[str] = []
    seen: set[str] = set()
    for s in ANCHORS + BELLWETHERS + (extra or []):
        if s not in seen:
            seen.add(s)
            universe.append(s)
    # Add S&P 500 constituents for breadth (cached in-process for 24h)
    try:
        for s in _get_sp500_tickers():
            if s not in seen:
                seen.add(s)
                universe.append(s)
    except Exception as e:  # pragma: no cover
        logger.warning("sp500 fetch skipped: %s", e)
    return universe


def _percent_change(symbol: str) -> tuple[float | None, float | None, int | None]:
    try:
        hist = yf.Ticker(symbol).history(period="5d", auto_adjust=False)
        if hist is None or hist.empty or len(hist) < 2:
            return None, None, None
        closes = hist["Close"].tolist()
        last, prev = closes[-1], closes[-2]
        if prev in (None, 0) or last is None:
            return None, None, None
        pct = (last - prev) / prev * 100
        vol = int(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None
        return float(last), float(pct), vol
    except Exception as e:  # pragma: no cover
        logger.debug("price fetch failed for %s: %s", symbol, e)
        return None, None, None


async def _recent_run_symbols(days: int = 30, limit: int = 50) -> list[str]:
    """Pull the unique tickers we've analyzed in the last N days from Postgres."""
    from webui.db import Run, get_sessionmaker
    sm = get_sessionmaker()
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        async with sm() as session:
            rows = (
                await session.execute(
                    select(Run.symbols).where(Run.created_at >= cutoff).limit(limit)
                )
            ).all()
        seen: set[str] = set()
        out: list[str] = []
        for (symbols,) in rows:
            for s in (symbols or []):
                s = (s or "").strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out
    except Exception as e:  # pragma: no cover
        logger.warning("recent_run_symbols failed: %s", e)
        return []


def get_movers(
    n_gainers: int = 8,
    n_losers: int = 8,
    extra: list[str] | None = None,
    pinned: list[str] | None = None,
    universe_limit: int = 200,
) -> list[dict[str, Any]]:
    """Compute the marquee feed.

    Strategy:
      1. Always include all ANCHORS first (real-time sentinels).
      2. Then user-pinned tickers (persisted client-side in localStorage),
         flagged 'pinned'.
      3. Then symbols from recent runs (auto from Postgres), flagged 'recent'.
      4. Score the BELLWETHERS + first `universe_limit` S&P 500 names by
         |% change|.
      5. Append top gainers (green) and top losers (red), interleaved.

    Cached for ~55s to avoid hammering yfinance.
    Returns a list of {"s","p","c","kind"} dicts.
    """
    now = time.time()
    if now - _cache["ts"] < _CACHE_TTL_S and _cache["data"]:
        return _cache["data"]

    universe = _build_universe(extra=extra)
    universe = universe[: max(len(ANCHORS) + len(BELLWETHERS), universe_limit)]

    feed: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Anchors come first (always visible)
    for s in ANCHORS:
        last, pct, _ = _percent_change(s)
        if last is None or pct is None:
            continue
        feed.append({"s": s.replace("^", ""), "p": last, "c": pct, "kind": "anchor"})
        seen.add(s)

    # 2. User-pinned (A)
    for s in (pinned or []):
        if s in seen:
            continue
        last, pct, _ = _percent_change(s)
        if last is None or pct is None:
            continue
        feed.append({"s": s.replace("^", ""), "p": last, "c": pct, "kind": "pinned"})
        seen.add(s)

    # 3. Recent run symbols (B) — caller passes via `extra` from /api/movers route
    #    (same yfinance backend; we tag them by checking against extra/recent at the route layer)

    # 4. Score everything else, pull top movers (C)
    scored: list[dict[str, Any]] = []
    for s in universe:
        if s in seen:
            continue
        last, pct, _ = _percent_change(s)
        if last is None or pct is None:
            continue
        scored.append({"s": s, "p": last, "c": pct})

    gainers = sorted(scored, key=lambda x: x["c"], reverse=True)[:n_gainers]
    losers = sorted(scored, key=lambda x: x["c"])[:n_losers]

    interleaved: list[dict[str, Any]] = []
    for i in range(max(len(gainers), len(losers))):
        if i < len(gainers):
            interleaved.append({**gainers[i], "kind": "gainer"})
        if i < len(losers):
            interleaved.append({**losers[i], "kind": "loser"})

    feed.extend(interleaved)

    _cache["ts"] = now
    _cache["data"] = feed
    return feed


def invalidate_cache() -> None:
    """Clear the in-process cache so the next /api/movers refresh recomputes."""
    _cache["ts"] = 0.0
    _cache["data"] = []

