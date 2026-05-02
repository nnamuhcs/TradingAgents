"""Market Scanner – multi-signal stock screener with LLM synthesis.

Layers:
  1. Multi-Factor Quantitative Screening  (500 → 30)
  2. Event-Driven Prioritization           (boost catalysts)
  3. Smart Money Signals                   (insider / institutional)
  4. LLM Synthesis                         (30 → 5-10 with reasoning)
"""

import io
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
import yfinance as yf
from langchain_core.messages import HumanMessage

from tradingagents.llm_clients import create_llm_client

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "XLB"]
MARKET_INDICES = ["SPY", "QQQ", "DIA", "IWM"]

_SP500_FALLBACK = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B", "UNH", "JNJ",
    "V", "XOM", "JPM", "PG", "MA", "HD", "CVX", "MRK", "ABBV", "LLY",
    "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO", "TMO", "ACN", "ABT",
    "DHR", "NEE", "PM", "LIN", "TXN", "CMCSA", "INTC", "AMD", "ORCL", "CRM",
    "NFLX", "UPS", "RTX", "HON", "QCOM", "LOW", "BA", "CAT", "GS", "AMGN",
]

# Cache S&P 500 list in-memory for the process lifetime
_sp500_cache: list[str] | None = None
_sp500_cache_time: float = 0
_SP500_CACHE_TTL = 86400  # 24 hours


# ── S&P 500 list ──────────────────────────────────────────────────────────────

def _get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 constituents (cached for 24h)."""
    global _sp500_cache, _sp500_cache_time
    if _sp500_cache and (time.time() - _sp500_cache_time) < _SP500_CACHE_TTL:
        return _sp500_cache

    sources = [
        # Wikipedia requires a real-browser UA; pandas.read_html alone gets 403
        ("wikipedia", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "Symbol"),
        # GitHub mirror that publishes the constituents as CSV / HTML
        ("datahub-html", "https://datahub.io/core/s-and-p-500-companies/r/0.html", "Symbol"),
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    for name, url, col in sources:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            tables = pd.read_html(io.StringIO(html), header=0)
            for t in tables:
                if col in t.columns:
                    tickers = [str(s).replace(".", "-").strip() for s in t[col].tolist() if s]
                    if len(tickers) > 100:
                        _sp500_cache = tickers
                        _sp500_cache_time = time.time()
                        logger.info(f"Loaded {len(tickers)} tickers from {name}")
                        return tickers
        except Exception as e:
            logger.warning(f"S&P 500 fetch from {name} failed: {e}")

    # Try datahub CSV as a final structured source
    try:
        req = urllib.request.Request(
            "https://datahub.io/core/s-and-p-500-companies/r/constituents.csv",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            csv_text = resp.read().decode("utf-8", errors="ignore")
        df = pd.read_csv(io.StringIO(csv_text))
        col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        tickers = [str(s).replace(".", "-").strip() for s in df[col].tolist() if s]
        if len(tickers) > 100:
            _sp500_cache = tickers
            _sp500_cache_time = time.time()
            logger.info(f"Loaded {len(tickers)} tickers from datahub CSV")
            return tickers
    except Exception as e:
        logger.warning(f"S&P 500 fetch from datahub CSV failed: {e}")

    logger.warning(f"All S&P 500 sources failed, using {len(_SP500_FALLBACK)}-ticker fallback")
    return list(_SP500_FALLBACK)


# ── Layer 1: Quantitative Screening ──────────────────────────────────────────

def _compute_rsi(closes: pd.Series, period: int = 14) -> float:
    """Compute RSI from a price series."""
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain.iloc[-1] / max(loss.iloc[-1], 1e-10)
    return float(100 - 100 / (1 + rs))


def _macd_crossover(closes: pd.Series) -> float:
    """Return MACD - Signal. Positive = bullish crossover."""
    ema12 = closes.ewm(span=12).mean()
    ema26 = closes.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    return float(macd.iloc[-1] - signal.iloc[-1])


def _screen_quant(sp500: list[str], spy_hist: pd.DataFrame) -> list[dict]:
    """Layer 1: score all S&P 500 stocks on quant factors. Returns scored list."""
    spy_1m = float(spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[-21] - 1) if len(spy_hist) >= 21 else 0
    spy_3m = float(spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[0] - 1) if len(spy_hist) > 1 else 0

    scored: list[dict] = []
    batch_size = 50
    for i in range(0, len(sp500), batch_size):
        batch = sp500[i : i + batch_size]
        logger.info(f"  Screening batch {i // batch_size + 1}/{(len(sp500) + batch_size - 1) // batch_size} ({len(batch)} stocks)")
        try:
            tickers_obj = yf.Tickers(" ".join(batch))
            for sym in batch:
                try:
                    hist = tickers_obj.tickers[sym].history(period="3mo")
                    if hist.empty or len(hist) < 21:
                        continue
                    closes = hist["Close"]
                    volumes = hist["Volume"]

                    # Relative strength vs SPY
                    perf_1m = float(closes.iloc[-1] / closes.iloc[-21] - 1)
                    perf_3m = float(closes.iloc[-1] / closes.iloc[0] - 1)
                    rs_1m = perf_1m - spy_1m
                    rs_3m = perf_3m - spy_3m
                    rs_score = rs_1m * 0.6 + rs_3m * 0.4  # weight recent more

                    # Volume breakout
                    avg_vol_20 = float(volumes.iloc[-20:].mean())
                    vol_today = float(volumes.iloc[-1])
                    vol_ratio = vol_today / max(avg_vol_20, 1)
                    vol_breakout = 1 if vol_ratio > 2.0 else vol_ratio / 2.0

                    # Price breakout
                    high_20 = float(closes.iloc[-20:].max())
                    sma_50 = float(closes.iloc[-50:].mean()) if len(closes) >= 50 else float(closes.mean())
                    at_20d_high = 1.0 if closes.iloc[-1] >= high_20 * 0.99 else 0.0
                    above_sma50 = 1.0 if closes.iloc[-1] > sma_50 else 0.0
                    price_score = at_20d_high * 0.5 + above_sma50 * 0.5

                    # Momentum (RSI + MACD)
                    rsi = _compute_rsi(closes)
                    rsi_score = 1.0 if 40 <= rsi <= 70 else max(0, 1 - abs(rsi - 55) / 30)
                    macd_diff = _macd_crossover(closes)
                    macd_score = min(1.0, max(0, macd_diff / max(abs(closes.iloc[-1]) * 0.01, 0.01)))

                    # Composite
                    total = (
                        rs_score * 30
                        + vol_breakout * 20
                        + price_score * 25
                        + rsi_score * 15
                        + macd_score * 10
                    )

                    scored.append({
                        "symbol": sym,
                        "score": round(total, 3),
                        "rs_1m": round(rs_1m * 100, 2),
                        "rs_3m": round(rs_3m * 100, 2),
                        "vol_ratio": round(vol_ratio, 2),
                        "rsi": round(rsi, 1),
                        "macd_signal": round(macd_diff, 4),
                        "at_20d_high": bool(at_20d_high),
                        "above_sma50": bool(above_sma50),
                        "close": round(float(closes.iloc[-1]), 2),
                        "pct_1m": round(perf_1m * 100, 2),
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"  Batch fetch failed: {e}")
            continue
        time.sleep(0.3)  # rate-limit courtesy

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ── Layer 2: Event-Driven Prioritization ──────────────────────────────────────

def _enrich_events(stocks: list[dict]) -> list[dict]:
    """Layer 2: boost scores for stocks with upcoming catalysts."""
    now = datetime.now()
    for item in stocks:
        sym = item["symbol"]
        event_boost = 0.0
        events: list[str] = []
        try:
            tk = yf.Ticker(sym)

            # Earnings in next 14 days
            try:
                cal = tk.calendar
                if cal is not None:
                    earnings_date = None
                    if isinstance(cal, pd.DataFrame) and "Earnings Date" in cal.index:
                        earnings_date = pd.Timestamp(cal.loc["Earnings Date"].iloc[0])
                    elif isinstance(cal, dict) and "Earnings Date" in cal:
                        ed = cal["Earnings Date"]
                        earnings_date = pd.Timestamp(ed[0]) if isinstance(ed, list) else pd.Timestamp(ed)
                    if earnings_date and 0 <= (earnings_date - pd.Timestamp(now)).days <= 14:
                        event_boost += 8
                        events.append(f"Earnings {earnings_date.strftime('%m/%d')}")
            except Exception:
                pass

            # Analyst recommendations
            try:
                recs = tk.recommendations
                if recs is not None and len(recs) > 0:
                    recent = recs.tail(3)
                    upgrades = sum(1 for _, r in recent.iterrows()
                                   if str(r.get("To Grade", "")).lower() in ("buy", "strong buy", "overweight", "outperform"))
                    if upgrades > 0:
                        event_boost += upgrades * 3
                        events.append(f"{upgrades} recent upgrade(s)")
            except Exception:
                pass

            # News via yfinance
            try:
                news = tk.news
                if news and len(news) > 0:
                    event_boost += min(len(news), 5) * 1.5
                    events.append(f"{len(news)} recent news items")
            except Exception:
                pass

        except Exception:
            pass

        item["event_boost"] = round(event_boost, 2)
        item["events"] = events
        item["total_score"] = round(item["score"] + event_boost, 3)
        time.sleep(0.1)

    stocks.sort(key=lambda x: x["total_score"], reverse=True)
    return stocks


# ── Layer 3: Smart Money Signals ──────────────────────────────────────────────

def _enrich_smart_money(stocks: list[dict]) -> list[dict]:
    """Layer 3: check insider and institutional signals."""
    for item in stocks:
        sym = item["symbol"]
        smart_boost = 0.0
        signals: list[str] = []
        try:
            tk = yf.Ticker(sym)

            # Insider transactions
            try:
                insiders = tk.insider_transactions
                if insiders is not None and len(insiders) > 0:
                    cutoff = datetime.now() - timedelta(days=30)
                    recent = insiders[pd.to_datetime(insiders.get("Start Date", insiders.iloc[:, 0]), errors="coerce") >= cutoff] if "Start Date" in insiders.columns else insiders.head(5)
                    buys = sum(1 for _, r in recent.iterrows()
                               if "purchase" in str(r.get("Transaction", r.get("Text", ""))).lower())
                    sells = sum(1 for _, r in recent.iterrows()
                                if "sale" in str(r.get("Transaction", r.get("Text", ""))).lower())
                    net = buys - sells
                    if net > 0:
                        smart_boost += net * 4
                        signals.append(f"Net insider buying ({buys}B/{sells}S)")
                    elif net < 0:
                        smart_boost -= 2
                        signals.append(f"Net insider selling ({buys}B/{sells}S)")
            except Exception:
                pass

            # Institutional holders
            try:
                inst = tk.institutional_holders
                if inst is not None and len(inst) > 0:
                    # Just having major institutional presence is mildly positive
                    signals.append(f"{len(inst)} institutional holders on file")
                    smart_boost += 1
            except Exception:
                pass

        except Exception:
            pass

        item["smart_money_boost"] = round(smart_boost, 2)
        item["smart_money_signals"] = signals
        item["total_score"] = round(item["total_score"] + smart_boost, 3)
        time.sleep(0.1)

    stocks.sort(key=lambda x: x["total_score"], reverse=True)
    return stocks


# ── Layer 4: LLM Synthesis ────────────────────────────────────────────────────

_SYNTHESIS_PROMPT = """You are an expert equity strategist. Analyze the following pre-screened stock candidates and select the 5-10 most compelling opportunities.

## Pre-Screening Summary
These {n} stocks were selected from the S&P 500 using quantitative screening (relative strength, volume breakouts, momentum), event catalysts, and smart money signals.

## Candidate Data
{candidates_json}

## Market Context
{market_context}

## Instructions
1. Consider the current macro environment (sector rotation, rate expectations, market regime)
2. Cross-reference quantitative signals with narrative/thematic reasoning
3. Identify which 5-10 stocks have the most compelling risk/reward setup
4. Explain WHY each pick was selected — what makes its combination of signals unique
5. Assign a conviction level: high, medium, or low

Return ONLY valid JSON (no markdown fences, no extra text):
{{
  "picks": [
    {{
      "symbol": "TICKER",
      "conviction": "high|medium|low",
      "reasoning": "1-2 sentence explanation"
    }}
  ],
  "market_regime": "Brief description of current market regime",
  "themes": ["Theme 1", "Theme 2"]
}}"""


def _llm_synthesize(llm, candidates: list[dict], market_context: str) -> dict:
    """Layer 4: LLM picks the best 5-10 with reasoning."""
    # Trim data for prompt (keep key fields)
    slim = []
    for c in candidates:
        slim.append({
            "symbol": c["symbol"],
            "close": c["close"],
            "pct_1m": c["pct_1m"],
            "rs_1m": c["rs_1m"],
            "rs_3m": c["rs_3m"],
            "vol_ratio": c["vol_ratio"],
            "rsi": c["rsi"],
            "at_20d_high": c["at_20d_high"],
            "above_sma50": c["above_sma50"],
            "events": c.get("events", []),
            "smart_money_signals": c.get("smart_money_signals", []),
            "total_score": c["total_score"],
        })

    prompt = _SYNTHESIS_PROMPT.format(
        n=len(slim),
        candidates_json=json.dumps(slim, indent=2),
        market_context=market_context,
    )

    response = llm.invoke([HumanMessage(content=prompt)])
    content = response.content if hasattr(response, "content") else str(response)

    try:
        text = content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0]
        return json.loads(text.strip())
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning(f"Failed to parse LLM synthesis: {e}")
        # Fallback: return top candidates by score
        return {
            "picks": [
                {"symbol": c["symbol"], "conviction": "medium", "reasoning": "Selected by quantitative score"}
                for c in candidates[:8]
            ],
            "market_regime": "Unknown (LLM parse failed)",
            "themes": [],
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_market_context(spy_hist: pd.DataFrame) -> str:
    """Build a short market context string from indices + sectors."""
    lines = []
    for etf in MARKET_INDICES + SECTOR_ETFS:
        try:
            hist = yf.Ticker(etf).history(period="5d")
            if len(hist) >= 2:
                pct = (hist["Close"].iloc[-1] / hist["Close"].iloc[-2] - 1) * 100
                lines.append(f"{etf}: ${hist['Close'].iloc[-1]:.2f} ({pct:+.2f}%)")
        except Exception:
            continue
    return "\n".join(lines) if lines else "Market context unavailable"


# ── Main Class ────────────────────────────────────────────────────────────────

class MarketScanner:
    """Multi-signal market scanner: quant screening → events → smart money → LLM synthesis."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.provider = provider or os.getenv("LLM_PROVIDER", "github-copilot")
        self.model = model or os.getenv("SCANNER_LLM", "claude-opus-4.7")
        client = create_llm_client(self.provider, self.model, base_url)
        self.llm = client.get_llm()
        self.progress_callback = progress_callback

    def _emit(self, **kwargs: Any) -> None:
        if self.progress_callback:
            try:
                self.progress_callback(dict(kwargs))
            except Exception as e:
                logger.debug("progress_callback raised: %s", e)

    def scan(self) -> dict:
        """Run full scan, return {symbols, reasoning, ...} (backward-compatible)."""
        result = self.scan_detailed()
        symbols = [p["symbol"] for p in result.get("picks", [])]
        reasoning_parts = [f"{p['symbol']} ({p['conviction']}): {p['reasoning']}" for p in result.get("picks", [])]
        return {
            "symbols": symbols,
            "reasoning": "; ".join(reasoning_parts),
            "market_data": result.get("market_data", {}),
            "timestamp": result.get("timestamp", datetime.now().isoformat()),
            "detailed": result,
        }

    def scan_detailed(self) -> dict:
        """Full multi-layer scan with detailed analysis."""
        logger.info("=" * 60)
        logger.info("MARKET SCANNER — Multi-Signal Analysis")
        logger.info("=" * 60)

        self._emit(layer=0, name="Init", status="started", info="Fetching SPY benchmark (3 months)")
        spy_hist = yf.Ticker("SPY").history(period="3mo")

        # Layer 1: Quantitative Screening
        sp500 = _get_sp500_tickers()
        logger.info(f"Layer 1: Screening {len(sp500)} S&P 500 stocks on quant factors...")
        self._emit(layer=1, name="Quant Screening", status="running",
                   input=len(sp500), output=None,
                   info=f"Scoring {len(sp500)} S&P 500 names on RS, vol breakouts, RSI/MACD")
        scored = _screen_quant(sp500, spy_hist)
        top30 = scored[:30]
        score_high = top30[0]["score"] if top30 else 0
        score_low = top30[-1]["score"] if top30 else 0
        logger.info(f"  → Top 30 selected (score range: {score_high:.1f} – {score_low:.1f})")
        self._emit(layer=1, name="Quant Screening", status="done",
                   input=len(sp500), output=len(top30),
                   info=f"Top 30 score range {score_high:.1f}–{score_low:.1f}",
                   symbols=[
                       {"s": s["symbol"], "score": round(s.get("score", 0), 1),
                        "rs_1m": s.get("rs_1m"), "vol_ratio": s.get("vol_ratio"),
                        "rsi": s.get("rsi"), "at_20d_high": bool(s.get("at_20d_high"))}
                       for s in top30
                   ])

        # Layer 2: Event-Driven Prioritization
        logger.info(f"Layer 2: Checking event catalysts for top {len(top30)} stocks...")
        self._emit(layer=2, name="Event Catalysts", status="running",
                   input=len(top30), output=None,
                   info="Earnings, upgrades, news catalysts")
        top30 = _enrich_events(top30)
        events_found = sum(1 for s in top30 if s.get("events"))
        logger.info(f"  → {events_found}/{len(top30)} stocks have event catalysts")
        self._emit(layer=2, name="Event Catalysts", status="done",
                   input=len(top30), output=events_found,
                   info=f"{events_found} of {len(top30)} have catalysts",
                   symbols=[
                       {"s": s["symbol"],
                        "events": list(s.get("events") or []),
                        "has_events": bool(s.get("events"))}
                       for s in top30
                   ])

        # Layer 3: Smart Money Signals
        logger.info(f"Layer 3: Checking smart money signals for top {len(top30)} stocks...")
        self._emit(layer=3, name="Smart Money", status="running",
                   input=len(top30), output=None,
                   info="Insider buys, institutional accumulation")
        top30 = _enrich_smart_money(top30)
        smart_found = sum(1 for s in top30 if s.get("smart_money_signals"))
        logger.info(f"  → {smart_found}/{len(top30)} stocks have smart money signals")
        self._emit(layer=3, name="Smart Money", status="done",
                   input=len(top30), output=smart_found,
                   info=f"{smart_found} of {len(top30)} flagged",
                   symbols=[
                       {"s": s["symbol"],
                        "smart_money": list(s.get("smart_money_signals") or []),
                        "has_smart_money": bool(s.get("smart_money_signals"))}
                       for s in top30
                   ])

        # Market context
        logger.info("Fetching market context (indices + sectors)...")
        market_ctx = _fetch_market_context(spy_hist)

        # Layer 4: LLM Synthesis
        logger.info("Layer 4: Running LLM synthesis to select final picks...")
        self._emit(layer=4, name="LLM Synthesis", status="running",
                   input=len(top30), output=None,
                   info=f"{self.model} reviewing all {len(top30)} candidates")
        synthesis = _llm_synthesize(self.llm, top30, market_ctx)
        picks = synthesis.get("picks", [])
        picked_set = {p["symbol"] for p in picks}
        logger.info(f"  → LLM selected {len(picks)} stocks")
        for p in picks:
            logger.info(f"    {p['symbol']} [{p['conviction']}]: {p['reasoning'][:80]}")
        self._emit(layer=4, name="LLM Synthesis", status="done",
                   input=len(top30), output=len(picks),
                   info=f"Selected {len(picks)} with conviction reasoning",
                   symbols=[
                       {"s": s["symbol"],
                        "picked": s["symbol"] in picked_set,
                        "conviction": next(
                            (p.get("conviction") for p in picks if p["symbol"] == s["symbol"]),
                            None,
                        )}
                       for s in top30
                   ])

        return {
            "picks": picks,
            "market_regime": synthesis.get("market_regime", ""),
            "themes": synthesis.get("themes", []),
            "candidates": top30,
            "all_scored": len(scored),
            "market_data": {
                "context": market_ctx,
            },
            "timestamp": datetime.now().isoformat(),
        }
