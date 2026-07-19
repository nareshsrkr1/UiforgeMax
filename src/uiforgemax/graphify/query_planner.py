"""Plan *short* Graphify searches from ticket context — not long NL essays.

Intelligence:
- Detect intent (theme/ui, api, structure) from summary + ACs
- Prefer compact keyword bags (fast on Windows ``graphify query``)
- Cap query count (theme → 0–2 NL; never one long question per AC)
- Honor IDE mediation ``graphSearchHints`` when present (short keywords /
  focus files produced at REQUIREMENT_ANALYSIS)
"""

from __future__ import annotations

import re
from typing import Any

# Max chars per graphify query argv — long strings are the main timeout driver.
_MAX_Q = 72


def plan_queries(
    requirements: dict[str, Any],
    visual_spec: dict[str, Any] | None,
    *,
    classification: dict[str, Any] | None = None,
    stack: dict[str, Any] | None = None,
) -> dict[str, Any]:
    classification = classification or {}
    stack = stack or {}
    surface = classification.get("surface") or requirements.get("surface") or "unknown"
    summary = (requirements.get("summary") or "").strip()
    acs = requirements.get("acceptanceCriteria") or []
    hints = requirements.get("graphSearchHints") or {}
    intent = _detect_intent(summary, acs, surface, hints)

    keywords = _merge_keywords(
        hints.get("keywords") or [],
        _style_keywords(summary, acs, visual_spec) if intent in ("theme_ui", "ui") else [],
        _token_keywords(summary, acs),
    )
    focus_files = [str(f) for f in (hints.get("focusFiles") or []) if f][:6]

    queries: list[dict[str, Any]] = []
    strategy = "lexical_first"

    if intent == "theme_ui":
        # Theme tickets: lexical+CSS scan is primary; at most one short NL probe.
        strategy = "lexical_first"
        bag = _short_bag(keywords or ["dark", "nav", "sidebar", "css", "theme"], focus_files)
        if bag:
            queries.append(
                {
                    "id": "Q-THEME",
                    "type": "keyword.query",
                    "question": bag,
                    "reason": "Short keyword probe for dark-mode / nav / styles",
                    "params": {"intent": intent},
                }
            )
    elif intent == "api":
        strategy = "hybrid"
        for need in (requirements.get("dataNeeds") or [])[:2]:
            entity = need.get("entity", "Resource")
            q = _clip(f"{entity} route handler api {(need.get('operations') or ['list'])[0]}")
            queries.append(
                {
                    "id": f"Q-API-{entity}"[:32],
                    "type": "keyword.query",
                    "question": q,
                    "reason": f"API surface for {entity}",
                }
            )
        if not queries:
            queries.append(
                {
                    "id": "Q-API",
                    "type": "keyword.query",
                    "question": _clip(_short_bag(keywords or ["api", "route", "handler"], focus_files)),
                    "reason": "API inventory keywords",
                }
            )
    else:
        strategy = "hybrid"
        bag = _short_bag(keywords or _token_keywords(summary, acs) or ["component", "page"], focus_files)
        queries.append(
            {
                "id": "Q-FOCUS",
                "type": "keyword.query",
                "question": bag or _clip(summary),
                "reason": "Compact focus keywords from ticket",
            }
        )
        if surface in ("ui_only", "full_stack", "unknown"):
            queries.append(
                {
                    "id": "Q-UI",
                    "type": "keyword.query",
                    "question": _clip("App.tsx styles.css pages layout sidebar"),
                    "reason": "UI shell inventory (short)",
                }
            )

    # Hard cap — theme: 1, hybrid: 2, never explode with per-AC NL
    max_q = 1 if strategy == "lexical_first" else 2
    if hints.get("shortQuestions"):
        # Mediation-supplied ultra-short questions win (already curated by IDE model).
        mediated = []
        for i, sq in enumerate(hints.get("shortQuestions") or []):
            text = _clip(str(sq))
            if not text:
                continue
            mediated.append(
                {
                    "id": f"Q-MED-{i+1}",
                    "type": "keyword.query",
                    "question": text,
                    "reason": "IDE graphSearchHints.shortQuestions",
                    "params": {"mediated": True},
                }
            )
        if mediated:
            queries = mediated[:max_q]

    # Optional Nx hint — replace last slot so it is not dropped by the cap
    if stack.get("nx") and intent != "theme_ui" and not hints.get("shortQuestions"):
        nx_q = {
            "id": "Q-NX",
            "type": "keyword.query",
            "question": "nx project app lib",
            "reason": "Nx boundary hint",
        }
        if len(queries) >= max_q:
            queries[-1] = nx_q
        else:
            queries.append(nx_q)

    queries = queries[:max_q]

    return {
        "queryCount": len(queries),
        "surface": surface,
        "intent": intent,
        "strategy": strategy,
        "keywords": keywords,
        "focusFiles": focus_files,
        "stack": {"primary": stack.get("primary"), "nx": bool(stack.get("nx"))},
        "queries": queries,
        "engine": "graphify-cli",
        "note": (
            "Short keyword queries + lexical_first for theme_ui. "
            "Long per-AC NL questions are intentionally not generated."
        ),
    }


def _detect_intent(
    summary: str,
    acs: list,
    surface: str,
    hints: dict[str, Any],
) -> str:
    if hints.get("intent") in ("theme_ui", "api", "ui", "general"):
        return str(hints["intent"])
    blob = " ".join(
        [summary.lower()]
        + [((a.get("text") if isinstance(a, dict) else str(a)) or "").lower() for a in acs]
    )
    theme_hits = sum(
        1
        for w in (
            "dark",
            "theme",
            "background",
            "sidebar",
            "nav",
            "color",
            "css",
            "style",
            "contrast",
            "palette",
        )
        if w in blob
    )
    if surface == "ui_only" and theme_hits >= 2:
        return "theme_ui"
    if surface == "api_only" or ("api" in blob and "route" in blob):
        return "api"
    if surface == "ui_only":
        return "ui"
    return "general"


def _token_keywords(summary: str, acs: list) -> list[str]:
    blob = " ".join(
        [summary]
        + [((a.get("text") if isinstance(a, dict) else str(a)) or "") for a in acs]
    )
    # Keep meaningful tokens only
    stop = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "into",
        "should",
        "must",
        "will",
        "have",
        "been",
        "using",
        "uses",
        "page",
        "existing",
        "change",
        "changes",
        "apply",
        "mode",
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


def _style_keywords(summary: str, acs: list, visual_spec: dict[str, Any] | None) -> list[str]:
    blob = " ".join(
        [summary.lower()]
        + [((a.get("text") if isinstance(a, dict) else str(a)) or "").lower() for a in acs]
    )
    keys = []
    for word in (
        "dark",
        "theme",
        "background",
        "sidebar",
        "navigation",
        "nav",
        "color",
        "css",
        "style",
        "contrast",
    ):
        if word in blob:
            keys.append(word)
    if visual_spec:
        keys.append("visual")
    return keys or ["style", "css", "theme"]


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
        # Use basename only — shorter and still matches graph source_file
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
