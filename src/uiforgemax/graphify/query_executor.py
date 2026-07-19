"""Execute planned questions via real ``graphify query`` against each root's graph.json."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from uiforgemax.graphify.cli_runner import run_query
from uiforgemax.graphify.lexical_fallback import (
    keywords_from_requirements,
    lexical_evidence_from_graph,
    supplement_style_files,
)


def execute_queries(
    query_plan: dict[str, Any],
    *,
    graphs_by_root: dict[str, Path],
    preferred_root: str | None = None,
    requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run each NL question against the appropriate graph.json.

    Soft-fails per query (timeout/error → empty evidence). When NL queries all
    time out or return empty, fills evidence via deterministic graph.json scan
    so requirement-map is not left with zero files.
    """
    roots = list(graphs_by_root.items())
    if preferred_root and preferred_root in graphs_by_root:
        roots = [(preferred_root, graphs_by_root[preferred_root])] + [
            (n, p) for n, p in graphs_by_root.items() if n != preferred_root
        ]

    strategy = (query_plan.get("strategy") or "hybrid").lower()
    max_nodes = _max_node_count(graphs_by_root)
    kw = list(query_plan.get("keywords") or []) or keywords_from_requirements(requirements)
    focus = list(query_plan.get("focusFiles") or [])

    # --- Lexical FIRST (fast, deterministic) ---
    lexical_nodes = _collect_lexical(roots, kw, focus)
    results: list[dict[str, Any]] = []
    if lexical_nodes:
        results.append(
            {
                "id": "Q-LEXICAL",
                "type": "lexical.graph_scan",
                "question": " ".join(kw[:8]) or "lexical scan",
                "reason": "Fast deterministic evidence before any graphify query",
                "params": {"keywords": kw, "focusFiles": focus, "strategy": strategy},
                "status": "ok",
                "answerText": f"lexical hits: {len(lexical_nodes)} file(s)",
                "nodes": lexical_nodes,
                "byRoot": [],
                "explanation": f"Lexical+CSS scan found {len(lexical_nodes)} file(s)",
            }
        )

    # theme_ui / lexical_first: skip slow NL when we already have enough files
    skip_nl = strategy == "lexical_first" and len(lexical_nodes) >= 2
    queries = [] if skip_nl else list(query_plan.get("queries") or [])
    max_nl = int(os.environ.get("UIFORGEMAX_GRAPHIFY_MAX_QUERIES", "2"))
    queries = queries[:max_nl]

    # Short keyword probes only — default 20s (was 25–40 with long NL).
    query_timeout = int(os.environ.get("UIFORGEMAX_GRAPHIFY_QUERY_TIMEOUT", "20"))
    timed_out = 0

    for q in queries:
        question = (q.get("question") or "")[:72]
        if not question.strip():
            continue
        per_root: list[dict[str, Any]] = []
        for name, graph_path in roots:
            answer = run_query(question, graph_path, timeout=query_timeout, budget=600)
            per_root.append({"root": name, **answer})

        best = next((a for a in per_root if a.get("status") == "ok" and a.get("nodes")), None)
        if best is None:
            best = next((a for a in per_root if a.get("status") == "empty"), None)
        if best is None:
            best = per_root[0] if per_root else {
                "status": "missing_graph",
                "answerText": "",
                "nodes": [],
            }
        if best.get("status") == "timeout":
            timed_out += 1

        results.append(
            {
                "id": q.get("id"),
                "type": q.get("type", "keyword.query"),
                "question": question,
                "reason": q.get("reason"),
                "params": q.get("params") or {},
                "status": best.get("status"),
                "answerText": best.get("answerText"),
                "nodes": best.get("nodes") or [],
                "byRoot": per_root,
                "explanation": _explain(best),
            }
        )

    with_nodes = sum(1 for r in results if r.get("nodes"))
    used_fallback = bool(lexical_nodes) or skip_nl

    # Last resort if lexical somehow empty and NL failed
    if with_nodes == 0 and roots:
        used_fallback = True
        lexical_nodes = _collect_lexical(roots, kw, focus)
        results.append(
            {
                "id": "Q-LEXICAL-FALLBACK",
                "type": "lexical.graph_scan",
                "question": "fallback lexical scan",
                "reason": "Recover evidence after empty NL",
                "params": {"keywords": kw},
                "status": "ok" if lexical_nodes else "empty",
                "answerText": f"lexical hits: {len(lexical_nodes)} file(s)",
                "nodes": lexical_nodes,
                "byRoot": [],
                "explanation": (
                    f"NL empty/timeouts={timed_out}; lexical found {len(lexical_nodes)} file(s)"
                ),
            }
        )

    return {
        "executed": len(results),
        "engine": "lexical-first+graphify" if used_fallback else "graphify-cli",
        "strategy": strategy,
        "skippedNl": skip_nl,
        "thinGraph": max_nodes < 40,
        "nodeCountHint": max_nodes,
        "nlTimeouts": timed_out,
        "usedLexicalFallback": used_fallback,
        "results": results,
    }


def _file_discovery_allowed() -> bool:
    """FS probes are opt-in — default is graph.json + graphify query only."""
    return os.environ.get("UIFORGEMAX_ALLOW_FILE_DISCOVERY", "").lower() in ("1", "true", "yes")


def _collect_lexical(
    roots: list[tuple[str, Path]],
    kw: list[str],
    focus: list[str],
) -> list[dict[str, str]]:
    fallback_nodes: list[dict[str, str]] = []
    allow_fs = _file_discovery_allowed()
    for _name, graph_path in roots:
        # Graph-only: score nodes already indexed in graph.json (not a directory walk).
        hits = lexical_evidence_from_graph(graph_path, keywords=kw)
        project_root = Path(graph_path).resolve().parent.parent
        hits = supplement_style_files(project_root, hits, allow_file_discovery=allow_fs)
        # focusFiles from mediation are keyword hints only unless FS discovery is enabled.
        if allow_fs:
            for rel in focus:
                rel_n = str(rel).replace("\\", "/")
                if (project_root / rel_n).is_file() and not any(
                    h.get("source_file") == rel_n for h in hits
                ):
                    hits.append(
                        {
                            "label": rel_n,
                            "source_file": rel_n,
                            "source_location": "focus-hint",
                        }
                    )
        fallback_nodes.extend(hits)
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for n in fallback_nodes:
        sf = n.get("source_file") or ""
        if sf and sf not in seen:
            seen.add(sf)
            deduped.append(n)
    return deduped


def _max_node_count(graphs_by_root: dict[str, Path]) -> int:
    best = 0
    for path in graphs_by_root.values():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            best = max(best, len(data.get("nodes") or []))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return best


def _explain(answer: dict[str, Any]) -> str:
    status = answer.get("status")
    nodes = answer.get("nodes") or []
    if status == "ok" and nodes:
        files = sorted({n.get("source_file") for n in nodes if n.get("source_file")})
        return f"Matched {len(nodes)} node(s); files: {', '.join(files[:8]) or '(none)'}"
    if status == "empty":
        return "No matching nodes in graph for this question"
    if status == "missing_graph":
        return "Graph file missing — run graphify update first"
    if status == "timeout":
        return "Query timed out — continuing with empty evidence for this question"
    return answer.get("error") or answer.get("answerText") or status or "unknown"
