"""Structured clarification answers at gates."""

from __future__ import annotations

import json

from uiforgemax.mcp_response import tool_response
from uiforgemax.tools.context import ToolContext


def _resolve_requirement_map_clarifications(run_dir, parsed: object) -> list[str]:
    """Mark matching (or all, if unkeyed) clarifications resolved=true on
    graph/requirement-map.json — this is the structure planning.py's hard
    blocker actually reads (``if not c.get("resolved")``). Recording an answer
    to a side file alone never cleared that blocker; the human could answer
    forever and the plan gate stayed stuck. Returns the ids marked resolved.
    """
    req_map_path = run_dir / "graph" / "requirement-map.json"
    if not req_map_path.exists():
        return []
    try:
        req_map = json.loads(req_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    clarifications = req_map.get("clarifications") or []
    if not clarifications:
        return []

    answer_keys = set(parsed.keys()) if isinstance(parsed, dict) else set()
    has_id_match = any(str(c.get("id")) in answer_keys for c in clarifications if c.get("id"))

    resolved_ids: list[str] = []
    for c in clarifications:
        cid = str(c.get("id") or "")
        if has_id_match:
            # Answers are keyed by clarification id — only resolve the ones named.
            if cid in answer_keys:
                c["resolved"] = True
                c["answer"] = parsed.get(cid)
                resolved_ids.append(cid)
        else:
            # Unkeyed / raw-text answer (or plain key=value not matching any id) —
            # a human explicitly called answer_clarifications, so treat it as
            # addressing every currently-open clarification rather than none.
            if not c.get("resolved"):
                c["resolved"] = True
                resolved_ids.append(cid or "?")

    req_map["clarifications"] = clarifications
    req_map_path.write_text(json.dumps(req_map, indent=2), encoding="utf-8")
    return resolved_ids


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

    resolved_ids = _resolve_requirement_map_clarifications(run_dir, parsed)

    state.artifacts["clarificationsAnswered"] = "graph/clarifications-answered.json"
    state.record(state.current_stage, "clarifications_answered", str(parsed)[:100])
    ctx.store.save(state)
    return tool_response(
        state,
        f"Clarifications recorded ({len(resolved_ids)} marked resolved on the requirement "
        "map). Call uiforgemax_advance to continue.",
    )
