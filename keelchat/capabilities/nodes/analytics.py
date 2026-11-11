"""Conversational analytics over one structured dataset. Each turn is a question; follow-ups resolve against the
last few questions and their SQL (kept in session outputs, without result rows)."""

from __future__ import annotations

from typing import Any

from keelchat.capabilities.nodes.common import repairs, tool_failed
from keelchat.capabilities.tools import ANALYTICS_REPAIRS
from keelchat.datasets.models import StructuredRef
from keelchat.registry.core import CapabilityConfig, Node, NodeResult, node
from keelchat.runtime.context import TurnContext

HISTORY = "analytics_history"
HISTORY_TURNS = 5
TOP_RECOMMENDATIONS = 3


class AnalyticsConfig(CapabilityConfig):
    dataset: StructuredRef


def reply_text(result: dict[str, Any]) -> str:
    """Summary plus the top recommendations. Generated text only; the full result travels as a detail item."""
    if result.get("status") == "failed":
        return "I couldn't analyse that. Could you rephrase the question or narrow it down?"
    lines = [result.get("summary") or "I couldn't find an answer in this data."]
    recs = result.get("recommendations") or []
    if recs:
        lines.append("Recommendations:")
        lines += [f"{i}. {r['action']}" for i, r in enumerate(recs[:TOP_RECOMMENDATIONS], 1)]
    return "\n".join(lines)


@node("analytics", tools=["analytics_query"], repairs=ANALYTICS_REPAIRS)
class Analytics(Node[AnalyticsConfig]):
    """Answers questions about a structured dataset with an AnalysisResult: the data, assumptions, findings, summary
    and recommendations. Stays open for follow-up questions."""

    config_model = AnalyticsConfig
    takes_questions = True
    timeout_s = 300.0  # one analysis: several model calls, a query and possibly sandboxed code

    async def act(self, ctx: TurnContext, config: AnalyticsConfig) -> NodeResult:
        history: list[dict[str, str]] = list(ctx.state.outputs.get(HISTORY) or [])
        args: dict[str, Any] = {"question": ctx.text, "dataset": config.dataset, "history": history[-HISTORY_TURNS:], "repairs": repairs(ctx, "analytics")}
        res = await ctx.services.gateway.call("analytics_query", args)
        ctx.tool_calls += 1
        if not res.ok:
            tool_failed(ctx, "analytics_query", res.error_kind)
            return NodeResult("I can't analyse the data right now. Please try again in a moment.")
        result: dict[str, Any] = dict(res.data or {})
        resolved = (result.get("input") or {}).get("resolved_question") or ctx.text
        sql = (result.get("data") or {}).get("sql") or ""
        outputs = {HISTORY: [*history, {"question": resolved, "sql": sql}][-HISTORY_TURNS:]} if sql else {}
        item = {"type": "analysis_result", "detail": True, "title": resolved, "result": result}
        return NodeResult(reply_text(result), items=[item], outputs=outputs)
