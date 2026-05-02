"""Per-run event bus. Each run has an asyncio.Queue that the trading graph
publishes to and the SSE endpoint subscribes to."""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Dict


class EventBus:
    def __init__(self) -> None:
        self._queues: Dict[str, asyncio.Queue] = {}
        self._closed: Dict[str, bool] = {}

    def open(self, run_id: str) -> None:
        self._queues[run_id] = asyncio.Queue()
        self._closed[run_id] = False

    def close(self, run_id: str) -> None:
        if run_id in self._queues:
            self._closed[run_id] = True
            self._queues[run_id].put_nowait({"event": "done", "data": {}})

    def has(self, run_id: str) -> bool:
        return run_id in self._queues

    def publish(self, run_id: str, event: str, data: dict) -> None:
        q = self._queues.get(run_id)
        if q is None or self._closed.get(run_id):
            return
        try:
            q.put_nowait({"event": event, "data": data})
        except asyncio.QueueFull:
            pass

    async def subscribe(self, run_id: str) -> AsyncIterator[str]:
        q = self._queues.get(run_id)
        if q is None:
            return
        while True:
            item = await q.get()
            yield self._format(item["event"], item["data"])
            if item["event"] == "done":
                break

    @staticmethod
    def _format(event: str, data: dict) -> str:
        # SSE wire format
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


bus = EventBus()
