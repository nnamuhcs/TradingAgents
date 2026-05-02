"""Market Scanner agent that uses yfinance + LLM to identify promising stocks.

Pulls S&P 500 top movers, sector ETF performance, and market indices,
then asks an LLM to reason about which stocks deserve deep analysis.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import yfinance as yf
from langchain_core.messages import HumanMessage

from tradingagents.llm_clients import create_llm_client

logger = logging.getLogger(__name__)

SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLC", "XLB"]
MARKET_INDICES = ["SPY", "QQQ", "DIA", "IWM"]

# S&P 500 tickers — fetched dynamically, with a hardcoded fallback subset
_SP500_FALLBACK = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B", "UNH", "JNJ",
    "V", "XOM", "JPM", "PG", "MA", "HD", "CVX", "MRK", "ABBV", "LLY",
    "PEP", "KO", "COST", "AVGO", "WMT", "MCD", "CSCO", "TMO", "ACN", "ABT",
    "DHR", "NEE", "PM", "LIN", "TXN", "CMCSA", "INTC", "AMD", "ORCL", "CRM",
    "NFLX", "UPS", "RTX", "HON", "QCOM", "LOW", "BA", "CAT", "GS", "AMGN",
]

SCANNER_PROMPT = """You are a professional equity market scanner. Analyze the following market data and identify 5-10 individual stocks that deserve deep fundamental + technical analysis today.

## Market Indices (recent performance)
{indices_data}

## Sector ETF Performance
{sector_data}

## S&P 500 Top Movers (by volume and price change)
{movers_data}

## Instructions
Based on this data, reason about:
1. Which sectors are showing momentum or rotation signals?
2. Which individual stocks are seeing unusual volume or price action?
3. Are there macro trends (risk-on/risk-off, sector rotation) suggesting opportunities?

Return ONLY a JSON object with this exact format (no markdown fences, no extra text):
{{"symbols": ["TICKER1", "TICKER2", ...], "reasoning": "Brief explanation of your picks"}}

Pick 5-10 symbols. Focus on actionable opportunities — stocks with clear catalysts, momentum shifts, or unusual activity. Prefer individual stocks over ETFs."""


def _get_sp500_tickers() -> list[str]:
    """Fetch S&P 500 constituents from Wikipedia, fall back to hardcoded list."""
    try:
        import pandas as pd
        table = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", header=0)
        tickers = table[0]["Symbol"].tolist()
        # Normalize BRK.B -> BRK-B for yfinance
        return [t.replace(".", "-") for t in tickers]
    except Exception as e:
        logger.warning(f"Failed to fetch S&P 500 list, using fallback: {e}")
        return _SP500_FALLBACK


def _fetch_quotes(symbols: list[str], period: str = "5d") -> dict:
    """Fetch recent price/volume data for a list of symbols."""
    results = {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                hist = tickers.tickers[sym].history(period=period)
                if hist.empty:
                    continue
                latest = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else hist.iloc[0]
                pct_change = ((latest["Close"] - prev["Close"]) / prev["Close"]) * 100
                results[sym] = {
                    "close": round(float(latest["Close"]), 2),
                    "volume": int(latest["Volume"]),
                    "pct_change": round(pct_change, 2),
                    "avg_volume": int(hist["Volume"].mean()),
                }
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Batch fetch failed, trying individually: {e}")
        for sym in symbols:
            try:
                t = yf.Ticker(sym)
                hist = t.history(period=period)
                if hist.empty:
                    continue
                latest = hist.iloc[-1]
                prev = hist.iloc[-2] if len(hist) > 1 else hist.iloc[0]
                pct_change = ((latest["Close"] - prev["Close"]) / prev["Close"]) * 100
                results[sym] = {
                    "close": round(float(latest["Close"]), 2),
                    "volume": int(latest["Volume"]),
                    "pct_change": round(pct_change, 2),
                    "avg_volume": int(hist["Volume"].mean()),
                }
            except Exception:
                continue
    return results


def _top_movers(quotes: dict, n: int = 30) -> dict:
    """Return top N movers by absolute pct change and volume ratio."""
    scored = []
    for sym, data in quotes.items():
        vol_ratio = data["volume"] / max(data["avg_volume"], 1)
        score = abs(data["pct_change"]) * vol_ratio
        scored.append((sym, data, score))
    scored.sort(key=lambda x: x[2], reverse=True)
    return {sym: data for sym, data, _ in scored[:n]}


def _format_quotes(quotes: dict) -> str:
    """Format quotes dict into a readable string for the LLM."""
    lines = []
    for sym, data in quotes.items():
        vol_ratio = data["volume"] / max(data["avg_volume"], 1)
        lines.append(
            f"  {sym}: ${data['close']} ({data['pct_change']:+.2f}%) "
            f"vol={data['volume']:,} (vol_ratio={vol_ratio:.1f}x)"
        )
    return "\n".join(lines) if lines else "  No data available"


class MarketScanner:
    """Scans the market using yfinance data + LLM reasoning to find stocks worth analyzing."""

    def __init__(
        self,
        provider: str = "github-copilot",
        model: str = "claude-opus-4.7",
        base_url: Optional[str] = None,
    ):
        client = create_llm_client(provider, model, base_url)
        self.llm = client.get_llm()
        self.provider = provider
        self.model = model

    def scan(self) -> dict:
        """Run the full market scan. Returns {"symbols": [...], "reasoning": "...", "market_data": {...}}."""
        logger.info("Fetching market data...")

        # 1. Market indices
        indices = _fetch_quotes(MARKET_INDICES)
        logger.info(f"Fetched {len(indices)} index quotes")

        # 2. Sector ETFs
        sectors = _fetch_quotes(SECTOR_ETFS)
        logger.info(f"Fetched {len(sectors)} sector ETF quotes")

        # 3. S&P 500 top movers
        sp500 = _get_sp500_tickers()
        logger.info(f"Fetching quotes for {len(sp500)} S&P 500 stocks...")
        all_quotes = _fetch_quotes(sp500)
        movers = _top_movers(all_quotes, n=30)
        logger.info(f"Identified {len(movers)} top movers")

        # 4. Ask LLM to pick stocks
        prompt = SCANNER_PROMPT.format(
            indices_data=_format_quotes(indices),
            sector_data=_format_quotes(sectors),
            movers_data=_format_quotes(movers),
        )

        logger.info("Asking LLM to identify promising stocks...")
        response = self.llm.invoke([HumanMessage(content=prompt)])
        content = response.content if hasattr(response, "content") else str(response)

        # Parse LLM response
        try:
            # Strip markdown fences if present
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                text = text.rsplit("```", 1)[0]
            result = json.loads(text.strip())
            symbols = result.get("symbols", [])
            reasoning = result.get("reasoning", "")
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse LLM response, extracting tickers: {e}")
            # Fallback: extract anything that looks like a ticker
            import re
            symbols = re.findall(r'\b([A-Z]{1,5})\b', content)
            # Filter to only known S&P 500 tickers
            sp500_set = set(sp500)
            symbols = [s for s in symbols if s in sp500_set][:10]
            reasoning = f"(Parsed from raw response) {content[:200]}"

        return {
            "symbols": symbols,
            "reasoning": reasoning,
            "market_data": {
                "indices": indices,
                "sectors": sectors,
                "top_movers": movers,
            },
            "timestamp": datetime.now().isoformat(),
        }
