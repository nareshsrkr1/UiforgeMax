"""Plan Graphify searches — mediation-driven with deterministic fallback.

Primary source: IDE-mediated query-strategy.json (from QUERY_STRATEGY mediation)
Secondary source: graphSearchStrategy in requirements.normalized.json (from REQUIREMENT_ANALYSIS)
Fallback: deterministic keyword extraction (stack-agnostic, no hardcoded patterns)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_MAX_Q = 72


def plan_queries(
    requirements: dict[str, Any],
    visual_spec: dict[str, Any] | None,
    *,
    classification: dict[str, Any] | None = None,
    stack: dict[str, Any] | None = None,
    subtask: dict[str, Any] | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    classification = classification or {}
    stack = stack or {}
    _subtask_id: str | None = subtask.get("id") if subtask else None

    # 1. Try mediated query strategy (from QUERY_STRATEGY mediation)
    mediated_strategy = _load_mediated_strategy(run_dir) if run_dir else None
    if mediated_strategy and mediated_strategy.get("queries"):
        return _plan_from_mediated_strategy(
            mediated_strategy, subtask_id=_subtask_id,
        )

    # 2. Try graphSearchStrategy from REQUIREMENT_ANALYSIS mediation
    search_strategy = requirements.get("graphSearchStrategy")
    if search_strategy and search_strategy.get("keywords"):
        return _plan_from_search_strategy(
            search_strategy, requirements, subtask=subtask,
            classification=classification, stack=stack,
        )

    # 3. Legacy: graphSearchHints (backward compat)
    hints = requirements.get("graphSearchHints")
    if hints and (hints.get("shortQuestions") or hints.get("keywords")):
        return _plan_from_legacy_hints(
            hints, requirements, subtask=subtask,
            classification=classification, stack=stack,
        )

    # 4. Deterministic fallback — extract from requirements text
    return _plan_deterministic(
        requirements, visual_spec, subtask=subtask,
        classification=classification, stack=stack,
    )


def _load_mediated_strategy(run_dir: Path | None) -> dict[str, Any] | None:
    if not run_dir:
        return None
    path = run_dir / "graph" / "query-strategy.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _plan_from_mediated_strategy(
    strategy: dict[str, Any],
    *,
    subtask_id: str | None = None,
) -> dict[str, Any]:
    """Build query plan directly from IDE-mediated strategy."""
    queries = []
    for q in strategy.get("queries") or []:
        question = _clip(q.get("question") or "")
        if not question:
            continue
        entry = {
            "id": q.get("id") or f"Q-MED-{len(queries)+1}",
            "type": "keyword.query",
            "question": question,
            "reason": q.get("reason") or "IDE mediated query",
            "params": {"mediated": True},
        }
        if q.get("subtaskId"):
            entry["params"]["subtaskId"] = q["subtaskId"]
        elif subtask_id:
            st_q = q.get("subtaskId")
            if st_q and st_q != subtask_id:
                continue
            entry["params"]["subtaskId"] = subtask_id
        queries.append(entry)

    if subtask_id:
        queries = [q for q in queries if q.get("params", {}).get("subtaskId") == subtask_id]

    return {
        "queryCount": len(queries),
        "surface": "mediated",
        "intent": "mediated",
        "strategy": strategy.get("strategy") or "hybrid",
        "keywords": strategy.get("lexicalKeywords") or [],
        "focusFiles": strategy.get("focusFiles") or [],
        "queries": queries[:6],
        "engine": "graphify-cli",
        "source": "mediated_query_strategy",
    }


def _plan_from_search_strategy(
    strategy: dict[str, Any],
    requirements: dict[str, Any],
    *,
    subtask: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    stack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build queries from REQUIREMENT_ANALYSIS graphSearchStrategy."""
    classification = classification or {}
    _subtask_id = subtask.get("id") if subtask else None
    surface = classification.get("surface") or "unknown"
    intent = strategy.get("intent") or "general"

    keywords = list(strategy.get("keywords") or [])
    component_names = list(strategy.get("componentNames") or [])
    focus_files = list(strategy.get("focusFiles") or [])[:6]

    if subtask:
        subtask_kws = list(subtask.get("focusKeywords") or [])
        keywords = _merge_keywords(subtask_kws, keywords)
        if subtask.get("summary"):
            component_names = _extract_names(subtask["summary"]) + component_names
        search_ctx = subtask.get("searchContext") or {}
        if search_ctx.get("componentNames"):
            component_names = list(search_ctx["componentNames"]) + component_names
        if search_ctx.get("searchQueries"):
            strategy = dict(strategy)
            existing_queries = list(strategy.get("searchQueries") or [])
            strategy["searchQueries"] = list(search_ctx["searchQueries"]) + existing_queries

    queries: list[dict[str, Any]] = []

    # Per-AC targeted queries (from perAcSearch)
    per_ac = strategy.get("perAcSearch") or []
    if subtask:
        linked = set(subtask.get("linkedAcIds") or [])
        if linked:
            per_ac = [p for p in per_ac if p.get("acId") in linked]

    for pac in per_ac[:2]:
        terms = pac.get("searchTerms") or []
        if terms:
            bag = _clip(" ".join(terms[:8]))
            queries.append({
                "id": f"Q-AC-{pac.get('acId', len(queries)+1)}",
                "type": "keyword.query",
                "question": bag,
                "reason": f"Targeted search for {pac.get('acId', 'AC')}",
                "params": {"acId": pac.get("acId"), "mediated": True},
            })

    # Search queries from mediation
    for i, sq in enumerate(strategy.get("searchQueries") or []):
        text = _clip(str(sq))
        if text and len(queries) < 3:
            queries.append({
                "id": f"Q-STRATEGY-{i+1}",
                "type": "keyword.query",
                "question": text,
                "reason": "From graphSearchStrategy.searchQueries",
                "params": {"mediated": True},
            })

    # Component name probe if no AC queries
    if not queries and component_names:
        bag = _clip(" ".join(component_names[:6] + keywords[:4]))
        queries.append({
            "id": "Q-COMPONENTS",
            "type": "keyword.query",
            "question": bag,
            "reason": "Component/module name search",
            "params": {"mediated": True},
        })

    # Keyword fallback
    if not queries and keywords:
        bag = _short_bag(keywords, focus_files)
        queries.append({
            "id": "Q-KEYWORDS",
            "type": "keyword.query",
            "question": bag,
            "reason": "Keyword search from graphSearchStrategy",
            "params": {"mediated": True},
        })

    max_q = 2 if intent in ("theme_ui",) else 3
    queries = queries[:max_q]

    if _subtask_id:
        for q in queries:
            q.setdefault("params", {})["subtaskId"] = _subtask_id
            if not q["id"].startswith(f"Q-{_subtask_id}-"):
                q["id"] = f"Q-{_subtask_id}-{q['id'].lstrip('Q-')}"

    all_keywords = _merge_keywords(keywords, component_names)
    return {
        "queryCount": len(queries),
        "surface": surface,
        "intent": intent,
        "strategy": "lexical_first" if intent == "theme_ui" else "hybrid",
        "keywords": all_keywords,
        "focusFiles": focus_files,
        "queries": queries,
        "engine": "graphify-cli",
        "source": "graph_search_strategy",
    }


def _plan_from_legacy_hints(
    hints: dict[str, Any],
    requirements: dict[str, Any],
    *,
    subtask: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    stack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward compat: build from old-style graphSearchHints."""
    classification = classification or {}
    _subtask_id = subtask.get("id") if subtask else None
    surface = classification.get("surface") or "unknown"
    intent = hints.get("intent") or "general"

    keywords = list(hints.get("keywords") or [])
    focus_files = list(hints.get("focusFiles") or [])[:6]

    if subtask:
        subtask_kws = list(subtask.get("focusKeywords") or [])
        keywords = _merge_keywords(subtask_kws, keywords)

    queries: list[dict[str, Any]] = []
    max_q = 1 if intent == "theme_ui" else 2

    for i, sq in enumerate(hints.get("shortQuestions") or []):
        text = _clip(str(sq))
        if text:
            queries.append({
                "id": f"Q-HINT-{i+1}",
                "type": "keyword.query",
                "question": text,
                "reason": "IDE graphSearchHints.shortQuestions",
                "params": {"mediated": True},
            })

    if not queries and keywords:
        bag = _short_bag(keywords, focus_files)
        queries.append({
            "id": "Q-KEYWORDS",
            "type": "keyword.query",
            "question": bag,
            "reason": "Keyword search from graphSearchHints",
        })

    queries = queries[:max_q]

    if _subtask_id:
        for q in queries:
            q.setdefault("params", {})["subtaskId"] = _subtask_id
            if not q["id"].startswith(f"Q-{_subtask_id}-"):
                q["id"] = f"Q-{_subtask_id}-{q['id'].lstrip('Q-')}"

    return {
        "queryCount": len(queries),
        "surface": surface,
        "intent": intent,
        "strategy": "lexical_first" if intent == "theme_ui" else "hybrid",
        "keywords": keywords,
        "focusFiles": focus_files,
        "queries": queries,
        "engine": "graphify-cli",
        "source": "legacy_hints",
    }


def _plan_deterministic(
    requirements: dict[str, Any],
    visual_spec: dict[str, Any] | None,
    *,
    subtask: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    stack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Last resort: extract keywords from requirement text. Stack-agnostic."""
    classification = classification or {}
    stack = stack or {}
    surface = classification.get("surface") or "unknown"
    summary = (requirements.get("summary") or "").strip()
    acs = requirements.get("acceptanceCriteria") or []

    if subtask:
        _subtask_id = subtask.get("id")
        if subtask.get("summary"):
            summary = subtask["summary"]
        if subtask.get("surfaceHint"):
            surface = subtask["surfaceHint"]
        linked_ac_ids = set(subtask.get("linkedAcIds") or [])
        if linked_ac_ids:
            acs = [ac for ac in acs if (ac.get("id") or "") in linked_ac_ids]
    else:
        _subtask_id = None

    subtask_kws = list(subtask.get("focusKeywords") or []) if subtask else []
    keywords = _merge_keywords(
        subtask_kws,
        _extract_names(summary),
        _token_keywords(summary, acs),
    )

    queries: list[dict[str, Any]] = []
    bag = _short_bag(keywords, [])
    if bag:
        queries.append({
            "id": "Q-FOCUS",
            "type": "keyword.query",
            "question": bag,
            "reason": "Extracted from requirement text (no mediation available)",
        })

    if _subtask_id:
        for q in queries:
            q.setdefault("params", {})["subtaskId"] = _subtask_id
            if not q["id"].startswith(f"Q-{_subtask_id}-"):
                q["id"] = f"Q-{_subtask_id}-{q['id'].lstrip('Q-')}"

    return {
        "queryCount": len(queries),
        "surface": surface,
        "intent": "general",
        "strategy": "hybrid",
        "keywords": keywords,
        "focusFiles": [],
        "queries": queries[:2],
        "engine": "graphify-cli",
        "source": "deterministic_fallback",
        "note": "No mediation available — using extracted keywords from requirement text.",
    }


def _extract_names(text: str) -> list[str]:
    """Extract likely component/class/module names (PascalCase, camelCase, snake_case)."""
    names = re.findall(r"\b[A-Z][a-zA-Z0-9]{2,}\b", text)
    names += re.findall(r"\b[a-z]+_[a-z_]+\b", text)
    return list(dict.fromkeys(n for n in names if len(n) > 2))[:10]


def _token_keywords(summary: str, acs: list) -> list[str]:
    blob = " ".join(
        [summary]
        + [((a.get("text") if isinstance(a, dict) else str(a)) or "") for a in acs]
    )
    stop = {
        "the", "and", "for", "with", "this", "that", "from", "into",
        "should", "must", "will", "have", "been", "using", "uses",
        "when", "then", "also", "each", "every", "only", "both",
        "can", "could", "would", "shall", "may", "might",
        "all", "any", "some", "more", "most", "other",
        "new", "existing", "current", "update", "add", "create",
        "implement", "ensure", "include", "display", "show",
    }
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", blob.lower())
    out: list[str] = []
    for t in tokens:
        if t in stop or t in out:
            continue
        out.append(t)
        if len(out) >= 10:
            break
    return out


def _merge_keywords(*groups: list[str]) -> list[str]:
    out: list[str] = []
    for g in groups:
        for k in g:
            k = str(k).strip().lower()
            if k and k not in out:
                out.append(k)
    return out[:12]


def _short_bag(keywords: list[str], focus_files: list[str]) -> str:
    parts: list[str] = []
    for f in focus_files[:3]:
        base = f.replace("\\", "/").split("/")[-1]
        if base and base not in parts:
            parts.append(base)
    for k in keywords:
        if k not in parts:
            parts.append(k)
        if len(" ".join(parts)) >= _MAX_Q - 4:
            break
    return _clip(" ".join(parts))


def _clip(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= _MAX_Q:
        return text
    return text[: _MAX_Q - 3].rstrip() + "..."
