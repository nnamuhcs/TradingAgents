"""Yahoo Finance WebSocket client (unofficial).

Yahoo's pricing stream lives at wss://streamer.finance.yahoo.com/ and
sends base64-encoded protobuf frames. The schema we care about is
roughly:
    string id          = 1;          // symbol
    float  price        = 2;
    int64  time         = 3;          // ms since epoch
    string currency     = 4;
    string exchange     = 5;
    int32  quoteType    = 6;          // 8=ETF, 9=EQUITY, ...
    string marketHours  = 7;
    float  changePercent = 8;
    int64  dayVolume    = 9;
    float  dayHigh       = 10;
    float  dayLow        = 11;
    float  change        = 12;
    int32  priceHint     = 13;

We don't need google.protobuf for a feed this small — we hand-decode
the wire format. This dodges a build/runtime dep on protoc and the
.proto file. The decoder ignores fields it doesn't recognize so a
schema change won't crash the service.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
from typing import Any

logger = logging.getLogger(__name__)


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not (b & 0x80):
            return n, pos
        shift += 7
        if shift >= 64:
            break
    raise ValueError("varint overflow / truncated")


def _decode_pricing(blob: bytes) -> dict[str, Any]:
    """Hand-roll a tiny protobuf wire decoder for the pricing message."""
    out: dict[str, Any] = {}
    pos = 0
    while pos < len(blob):
        tag, pos = _decode_varint(blob, pos)
        field_no = tag >> 3
        wire_type = tag & 0x7
        if wire_type == 0:  # varint
            v, pos = _decode_varint(blob, pos)
            if field_no == 3:
                out["time_ms"] = v
            elif field_no == 6:
                out["quoteType"] = v
            elif field_no == 9:
                out["dayVolume"] = v
            elif field_no == 13:
                out["priceHint"] = v
        elif wire_type == 1:  # 64-bit (double)
            (val,) = struct.unpack_from("<d", blob, pos)
            pos += 8
        elif wire_type == 2:  # length-delimited
            length, pos = _decode_varint(blob, pos)
            data = blob[pos : pos + length]
            pos += length
            try:
                s = data.decode("utf-8")
            except UnicodeDecodeError:
                s = ""
            if field_no == 1:
                out["symbol"] = s
            elif field_no == 4:
                out["currency"] = s
            elif field_no == 5:
                out["exchange"] = s
            elif field_no == 7:
                out["marketHours"] = s
        elif wire_type == 5:  # 32-bit (float)
            (val,) = struct.unpack_from("<f", blob, pos)
            pos += 4
            if field_no == 2:
                out["price"] = float(val)
            elif field_no == 8:
                out["changePercent"] = float(val)
            elif field_no == 10:
                out["dayHigh"] = float(val)
            elif field_no == 11:
                out["dayLow"] = float(val)
            elif field_no == 12:
                out["change"] = float(val)
        else:
            # Unknown wire type — can't safely skip without schema info
            break
    return out


class YahooLiveTicker:
    """Maintains a snapshot dict of latest prices for a fixed symbol set,
    fed by Yahoo's WebSocket stream. Falls back to no-op if the stream
    can't be established (caller can still use yfinance polling)."""

    URL = "wss://streamer.finance.yahoo.com/"

    def __init__(self) -> None:
        self.snapshot: dict[str, dict[str, Any]] = {}
        self.subscribed: set[str] = set()
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._connected = False
        self._last_msg_ts: float = 0.0

    async def start(self, symbols: list[str]) -> None:
        self.subscribed = set(symbols)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def update_subscriptions(self, symbols: list[str]) -> None:
        new_set = set(symbols)
        if new_set != self.subscribed:
            self.subscribed = new_set
            if self._ws is not None and self._connected:
                try:
                    import json
                    await self._ws.send(json.dumps({"subscribe": list(new_set)}))
                except Exception as e:
                    logger.warning("yahoo-ws resubscribe failed: %s", e)

    async def _loop(self) -> None:
        backoff = 2.0
        while True:
            try:
                await self._connect_and_read()
                backoff = 2.0
            except Exception as e:
                logger.warning("yahoo-ws disconnected (%s); reconnecting in %.1fs", e, backoff)
                self._connected = False
                self._ws = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 60.0)

    async def _connect_and_read(self) -> None:
        try:
            from websockets.asyncio.client import connect  # websockets>=13
        except ImportError:  # pragma: no cover
            from websockets.client import connect  # type: ignore

        import json

        async with connect(self.URL, ping_interval=15, max_size=2**20) as ws:
            self._ws = ws
            self._connected = True
            await ws.send(json.dumps({"subscribe": list(self.subscribed)}))
            logger.info("yahoo-ws connected, subscribed to %d symbols", len(self.subscribed))
            async for raw in ws:
                self._last_msg_ts = time.time()
                try:
                    if isinstance(raw, bytes):
                        blob = raw
                    else:
                        blob = base64.b64decode(raw)
                    msg = _decode_pricing(blob)
                    sym = msg.get("symbol")
                    if not sym:
                        continue
                    self.snapshot[sym] = {
                        "s": sym.replace("^", ""),
                        "p": msg.get("price"),
                        "c": msg.get("changePercent"),
                        "t": msg.get("time_ms"),
                    }
                except Exception as e:
                    logger.debug("yahoo-ws decode failed: %s", e)

    @property
    def is_live(self) -> bool:
        return self._connected and (time.time() - self._last_msg_ts) < 120

    def get_snapshot(self) -> dict[str, dict[str, Any]]:
        return dict(self.snapshot)


# Module-level singleton
ticker = YahooLiveTicker()
