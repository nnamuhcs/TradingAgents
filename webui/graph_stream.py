"""Stream the LangGraph trading-graph chunks and emit structured events.

This is the WebUI-facing equivalent of cli/main.py's `run_analysis` loop.
The TUI used the message_buffer / Live layout approach; here we publish
the same semantic events to an EventBus so the browser can render its own
3-pane layout.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

logger = logging.getLogger(__name__)


# All agents in the workflow, in the order they appear in the TUI status panel
AGENT_ORDER: list[str] = [
    "Market Analyst",
    "Social Analyst",
    "News Analyst",
    "Fundamentals Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Neutral Analyst",
    "Conservative Analyst",
    "Portfolio Manager",
]

ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}

ANALYST_REPORT_KEY = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def _classify(message: Any) -> tuple[str, str | None]:
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                v = c.get("text") or c.get("content")
                if v:
                    parts.append(str(v))
            else:
                parts.append(str(c))
        content_str = " ".join(p for p in parts if p)
    else:
        content_str = str(content) if content else ""
    content_str = (content_str or "").strip() or None

    if isinstance(message, HumanMessage):
        if content_str and content_str == "Continue":
            return ("Control", content_str)
        return ("User", content_str)
    if isinstance(message, ToolMessage):
        return ("Data", content_str)
    if isinstance(message, AIMessage):
        return ("Agent", content_str)
    return ("System", content_str)


class GraphStreamer:
    """Holds per-run state and emits events as it processes graph chunks."""

    def __init__(
        self,
        publish: Callable[[str, dict], None],
        selected_analysts: Iterable[str],
    ) -> None:
        self._publish = publish
        self._processed_message_ids: set[str] = set()
        self._reports: dict[str, str] = {}
        self._statuses: dict[str, str] = {a: "pending" for a in AGENT_ORDER}
        # Mark non-selected analysts as skipped
        selected_lower = {s.lower() for s in selected_analysts}
        for short, full in ANALYST_AGENT_NAMES.items():
            if short not in selected_lower:
                self._statuses[full] = "skipped"

    # ────────────────────────── public API ──────────────────────────

    def emit_initial(self, ticker: str, analysis_date: str) -> None:
        """Send the initial agent-status panel + system messages."""
        self._publish("agent_states", {"states": dict(self._statuses), "order": AGENT_ORDER})
        self._publish("message", {"type": "System", "content": f"Selected ticker: {ticker}"})
        self._publish("message", {"type": "System", "content": f"Analysis date: {analysis_date}"})
        # Mark the first not-skipped analyst as in_progress
        for short in ("market", "social", "news", "fundamentals"):
            full = ANALYST_AGENT_NAMES[short]
            if self._statuses.get(full) == "pending":
                self._set_status(full, "in_progress")
                break

    def emit_chunk(self, chunk: dict[str, Any]) -> None:
        """Process one graph stream chunk; publish derived events."""
        # 1) New messages (Agent / Tool / User / System)
        for message in chunk.get("messages", []) or []:
            mid = getattr(message, "id", None)
            if mid is not None:
                if mid in self._processed_message_ids:
                    continue
                self._processed_message_ids.add(mid)
            mtype, content = _classify(message)
            if content:
                self._publish("message", {"type": mtype, "content": content})

            tool_calls = getattr(message, "tool_calls", None)
            if tool_calls:
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        self._publish("tool_call", {"tool": tc.get("name"), "args": tc.get("args")})
                    else:
                        self._publish("tool_call", {"tool": tc.name, "args": getattr(tc, "args", {})})

        # 2) Analyst reports — drive status based on accumulated chunks
        for short, key in ANALYST_REPORT_KEY.items():
            full = ANALYST_AGENT_NAMES[short]
            if self._statuses.get(full) == "skipped":
                continue
            new_content = chunk.get(key)
            if new_content and new_content != self._reports.get(key):
                self._reports[key] = new_content
                self._publish("report_section", {"section": key, "content": new_content})
                # mark this analyst completed and the next one in_progress
                if self._statuses.get(full) != "completed":
                    self._set_status(full, "completed")
                    self._advance_next_analyst(short)

        # 3) Investment debate (Bull / Bear / Research Manager)
        debate = chunk.get("investment_debate_state") or {}
        bull = (debate.get("bull_history") or "").strip()
        bear = (debate.get("bear_history") or "").strip()
        judge_inv = (debate.get("judge_decision") or "").strip()
        if bull or bear:
            for full in ("Bull Researcher", "Bear Researcher", "Research Manager"):
                if self._statuses.get(full) == "pending":
                    self._set_status(full, "in_progress")
        if bull:
            self._publish("report_section", {"section": "investment_plan",
                                             "content": f"### Bull Researcher\n\n{bull}"})
        if bear:
            self._publish("report_section", {"section": "investment_plan",
                                             "content": f"### Bear Researcher\n\n{bear}"})
        if judge_inv:
            self._publish("report_section", {"section": "investment_plan",
                                             "content": f"### Research Manager\n\n{judge_inv}"})
            for full in ("Bull Researcher", "Bear Researcher", "Research Manager"):
                self._set_status(full, "completed")
            self._set_status("Trader", "in_progress")

        # 4) Trader plan
        trader_plan = chunk.get("trader_investment_plan")
        if trader_plan and trader_plan != self._reports.get("trader_investment_plan"):
            self._reports["trader_investment_plan"] = trader_plan
            self._publish("report_section", {"section": "trader_investment_plan", "content": trader_plan})
            if self._statuses.get("Trader") != "completed":
                self._set_status("Trader", "completed")
                self._set_status("Aggressive Analyst", "in_progress")

        # 5) Risk debate
        risk = chunk.get("risk_debate_state") or {}
        agg = (risk.get("aggressive_history") or "").strip()
        neu = (risk.get("neutral_history") or "").strip()
        con = (risk.get("conservative_history") or "").strip()
        judge_risk = (risk.get("judge_decision") or "").strip()
        if agg and self._statuses.get("Aggressive Analyst") == "pending":
            self._set_status("Aggressive Analyst", "in_progress")
        if neu and self._statuses.get("Neutral Analyst") == "pending":
            self._set_status("Neutral Analyst", "in_progress")
        if con and self._statuses.get("Conservative Analyst") == "pending":
            self._set_status("Conservative Analyst", "in_progress")
        if agg:
            self._publish("report_section", {"section": "final_trade_decision",
                                             "content": f"### Aggressive Analyst\n\n{agg}"})
        if neu:
            self._publish("report_section", {"section": "final_trade_decision",
                                             "content": f"### Neutral Analyst\n\n{neu}"})
        if con:
            self._publish("report_section", {"section": "final_trade_decision",
                                             "content": f"### Conservative Analyst\n\n{con}"})
        if judge_risk:
            self._publish("report_section", {"section": "final_trade_decision",
                                             "content": f"### Portfolio Manager\n\n{judge_risk}"})
            for full in ("Aggressive Analyst", "Neutral Analyst", "Conservative Analyst", "Portfolio Manager"):
                self._set_status(full, "completed")

    def emit_done(self) -> None:
        for full in AGENT_ORDER:
            if self._statuses.get(full) not in ("completed", "skipped"):
                self._set_status(full, "completed")

    # ────────────────────────── internals ──────────────────────────

    def _set_status(self, agent: str, status: str) -> None:
        if self._statuses.get(agent) == status:
            return
        self._statuses[agent] = status
        self._publish("agent_status", {"agent": agent, "status": status})

    def _advance_next_analyst(self, just_done: str) -> None:
        order = ("market", "social", "news", "fundamentals")
        try:
            idx = order.index(just_done)
        except ValueError:
            return
        for nxt in order[idx + 1:]:
            full = ANALYST_AGENT_NAMES[nxt]
            if self._statuses.get(full) == "pending":
                self._set_status(full, "in_progress")
                return
        # all analysts done → kick off research team
        for full in ("Bull Researcher", "Bear Researcher"):
            if self._statuses.get(full) == "pending":
                self._set_status(full, "in_progress")
                return
