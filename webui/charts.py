"""Chart data: OHLCV + simple RSI/MACD JSON for Plotly."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

import yfinance as yf


def get_ohlcv(symbol: str, period: str = "6mo") -> dict[str, Any]:
    hist = yf.Ticker(symbol).history(period=period)
    if hist.empty:
        return {"symbol": symbol, "ohlc": [], "volume": [], "rsi": [], "macd": []}

    dates = [d.strftime("%Y-%m-%d") for d in hist.index]
    closes = hist["Close"].tolist()
    rsi = _rsi(closes, 14)
    macd, signal = _macd(closes)

    return {
        "symbol": symbol,
        "dates": dates,
        "open": _round(hist["Open"].tolist()),
        "high": _round(hist["High"].tolist()),
        "low": _round(hist["Low"].tolist()),
        "close": _round(closes),
        "volume": [int(v) for v in hist["Volume"].tolist()],
        "rsi": _round(rsi, 1),
        "macd": _round(macd, 3),
        "macd_signal": _round(signal, 3),
    }


def _round(xs: list[float], digits: int = 2) -> list[float | None]:
    return [None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), digits) for x in xs]


def _rsi(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) < period + 1:
        return [None] * len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis: list[float | None] = [None] * (period)
    rsis.append(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = 100.0 if avg_loss == 0 else avg_gain / avg_loss
        rsis.append(100 - 100 / (1 + rs))
    return rsis


def _ema(xs: list[float], period: int) -> list[float | None]:
    if len(xs) < period:
        return [None] * len(xs)
    k = 2 / (period + 1)
    out: list[float | None] = [None] * (period - 1)
    out.append(sum(xs[:period]) / period)
    for x in xs[period:]:
        prev = out[-1]
        out.append(x * k + prev * (1 - k))  # type: ignore[operator]
    return out


def _macd(closes: list[float]) -> tuple[list[float | None], list[float | None]]:
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(ema12, ema26)
    ]
    valid = [m for m in macd if m is not None]
    sig = _ema(valid, 9)
    pad = len(macd) - len(sig)
    signal = [None] * pad + sig
    return macd, signal
