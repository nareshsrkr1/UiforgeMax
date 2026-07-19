"""Structured clarification answers at gates."""

from __future__ import annotations

import json

from uiforgemax.mcp_response import tool_response
from uiforgemax.tools.context import ToolContext


def answer_clarifications(ctx: ToolContext, run_id: str, answers: str) -> str:
    """Record human answers to graph clarifications (JSON dict or key=value lines)."""
    state = ctx.store.load(run_id)
    run_dir = ctx.store.run_dir(run_id)
    path = run_dir / "graph" / "clarifications-answered.json"

    try:
        parsed = json.loads(answers)
    except json.JSONDecodeError:
        parsed = {"raw": answers}

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    state.artifacts["clarificationsAnswered"] = "graph/clarifications-answered.json"
    state.record(state.current_stage, "clarifications_answered", str(parsed)[:100])
    ctx.store.save(state)
    return tool_response(state, "Clarifications recorded. Call uiforgemax_advance to continue.")
