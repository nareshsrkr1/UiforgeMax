"""Normalize intake into visual-spec and requirements (LLM-free deterministic mediation)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.classify import is_greenfield
from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

# Sentinel value written into visual-spec.json when images are present but
# VISUAL_INTERPRETATION mediation has not yet confirmed the spec.  Downstream
# stages and mediation prompts check for this so they know the spec is
# provisional and must not be used as authoritative layout/component data.
_VISUAL_SPEC_UNCONFIRMED_SENTINEL = "__UNCONFIRMED_PENDING_MEDIATION__"

OVERRIDE_PATTERNS = [
    re.compile(r"override:\s*(.+)", re.I),
    re.compile(r"don't\s+create\s+(.+)", re.I),
    re.compile(r"do not create\s+(.+)", re.I),
    re.compile(r"use\s+(green|blue|red)\s+(?:primary\s+)?theme", re.I),
    re.compile(r"change\s+(.+?)\s+to\s+(.+)", re.I),
    re.compile(r"don't\s+use\s+(.+)", re.I),
    re.compile(r"remove\s+(.+)", re.I),
]


def parse_jira_overrides(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for comment in comments:
        body = comment.get("body", "")
        out.append(
            {
                "author": comment.get("author"),
                "created": comment.get("created"),
                "body": body,
                "isOverride": any(p.search(body) for p in OVERRIDE_PATTERNS),
            }
        )
    return out


def build_visual_spec(
    filename: str,
    prompt: str | None = None,
    sot: dict[str, Any] | None = None,
    has_image: bool = False,
    *,
    run_dir: Path | None = None,
    intake_text: str = "",
) -> dict[str, Any]:
    """Build a visual spec from actual intake inputs — never invent demo UI.

    When images are present, we mark the spec as ``visualSpecUnconfirmed=True``
    because only VISUAL_INTERPRETATION mediation (the IDE model reading the
    actual image) can produce authoritative layout/component/token data.  MCP
    writes a minimal structural shell so the artifact exists on disk; mediation
    must overwrite it with real content before PLAN_REFINEMENT fires.

    For HTML-only input, we extract a DOM summary mechanically (no LLM) and
    mark the spec as confirmed with lower confidence until mediation reviews.
    """
    sot = sot or {}
    primary_html = sot.get("primaryHtml")
    primary_image = sot.get("primaryImage") or (filename if has_image else None)
    design_notes = sot.get("designNotes") or []
    reference_artifacts = sot.get("attachments") or []

    # When we have images, emit a provisional shell that forces mediation.
    # The IDE model will replace components/layout/tokens with what it sees.
    if has_image:
        return {
            "source": primary_image or filename,
            "confidence": 0.0,
            "visualSpecUnconfirmed": True,
            "pendingMediation": _VISUAL_SPEC_UNCONFIRMED_SENTINEL,
            "note": (
                "VISUAL_INTERPRETATION mediation is required — this shell must be replaced "
                "by the IDE model reading the actual image. Do not use layout/components "
                "from this stub in PLAN_REFINEMENT."
            ),
            "layout": {"regions": []},
            "components": [],
            "interactions": [],
            "visualTokens": {"colors": [], "typography": [], "spacing": []},
            "uncertainFields": ["all — awaiting VISUAL_INTERPRETATION mediation"],
            "referenceHtml": primary_html,
            "referenceImage": primary_image,
            "referenceArtifacts": reference_artifacts,
            "designNotes": design_notes,
            "sourceOfTruth": sot,
        }

    # HTML-only: do a lightweight mechanical extract (no LLM) — headings, links,
    # form fields, button labels — as a structural skeleton for mediation.
    if primary_html:
        scope_text = intake_text or (prompt or "")
        html_summary = _extract_html_structure(
            primary_html, run_dir=run_dir, intake_text=scope_text
        )
        note = (
            "Derived mechanically from inputs/page.html. VISUAL_INTERPRETATION mediation "
            "refines component list, shell/sidebar geometry, tokens, and interactions "
            "against the HTML source of truth."
        )
        if html_summary.get("primaryHtmlView"):
            note += (
                f" Multi-view HTML scoped to `{html_summary['primaryHtmlView']}` "
                f"from intake text; sibling screens are out of scope."
            )
        elif html_summary.get("htmlViewScopeAmbiguous"):
            note += (
                " Multi-view HTML detected but intake did not uniquely name a screen — "
                "mediation must choose the primary view."
            )
        return {
            "source": primary_html,
            "confidence": 0.6,
            "visualSpecUnconfirmed": False,
            "htmlDerived": True,
            "note": note,
            **html_summary,
            "referenceHtml": primary_html,
            "referenceImage": primary_image,
            "referenceArtifacts": reference_artifacts,
            "designNotes": design_notes,
            "sourceOfTruth": sot,
        }

    # Prompt-only or Jira-only — no visual source at all.  Build a minimal
    # intent-derived spec from prompt keywords; no layout regions or component
    # lists since we have nothing to derive them from.
    intent_summary = _extract_prompt_intent(prompt or "")
    return {
        "source": filename,
        "confidence": 0.3,
        "visualSpecUnconfirmed": False,
        "promptDerived": True,
        "note": (
            "No image or HTML available — spec built from prompt intent only. "
            "REQUIREMENT_ANALYSIS mediation must produce the actual design."
        ),
        **intent_summary,
        "referenceHtml": primary_html,
        "referenceImage": primary_image,
        "referenceArtifacts": reference_artifacts,
        "designNotes": design_notes,
        "sourceOfTruth": sot,
    }


def _plain_html_text(inner: str, unescape: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", inner)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _iter_js_function_bodies(raw: str) -> list[tuple[str, str]]:
    """Return ``(functionName, body)`` for top-level ``function name(){…}`` blocks."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r"function\s+(\w+)\s*\([^)]*\)\s*\{", raw):
        name = m.group(1)
        start = m.end()
        depth = 1
        i = start
        while i < len(raw) and depth > 0:
            ch = raw[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        out.append((name, raw[start:i]))
    return out


# Hero / page-title helpers — matched by NAME SHAPE (the identifier contains
# hero/title/header/heading/banner), NOT a fixed product-specific allowlist, so
# any codebase's hero helper is recognized (shHero, sectionHeader, pageTitle, …).
# First string arg = the title; second = the subtitle.
_HERO_CALL_RE = re.compile(
    r"\b\w*(?:hero|title|header|heading|banner)\w*\s*\(\s*[\"']([^\"'\n]{2,80})[\"']",
    re.I,
)
_HERO_CALL_2ND_RE = re.compile(
    r"\b\w*(?:hero|title|header|heading|banner)\w*\s*\(\s*"
    r"[\"'][^\"'\n]+[\"']\s*,\s*[\"']([^\"'\n]{2,100})[\"']",
    re.I,
)
# Quote-aware <button> matcher: an attribute value can itself contain '>' — e.g.
# an inline arrow-function handler `onclick="()=>{…}"` — which a naive `[^>]*`
# stops at, corrupting the captured label. Consuming quoted runs wholesale keeps
# the real label intact. Generic across any HTML/JS-template mockup.
_BUTTON_RE = re.compile(
    r"<button\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>(.*?)</button>",
    re.I | re.S,
)


def _name_tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    spaced = spaced.replace("_", " ").replace("-", " ")
    return [t.lower() for t in re.findall(r"[a-z0-9]{3,}", spaced.lower())]


def _extract_html_views(raw: str, *, unescape: Any) -> list[dict[str, Any]]:
    """Detect multi-screen mockup views from JS render functions + hero titles."""
    views: list[dict[str, Any]] = []
    for name, body in _iter_js_function_bodies(raw):
        titles: list[str] = []
        seen: set[str] = set()

        def _add_title(text: str) -> None:
            t = re.sub(r"\s+", " ", (text or "").strip())
            if not t or not _looks_like_ui_copy(t):
                return
            key = t.lower()
            if key in seen:
                return
            seen.add(key)
            titles.append(t)

        for hm in _HERO_CALL_RE.finditer(body):
            _add_title(unescape(hm.group(1)))
        for hm in _HERO_CALL_2ND_RE.finditer(body):
            _add_title(unescape(hm.group(1)))
        for hm in re.finditer(r"<h1\b[^>]*>(.*?)</h1>", body, re.I | re.S):
            _add_title(_plain_html_text(hm.group(1), unescape))

        buttons: list[str] = []
        for bm in _BUTTON_RE.finditer(body):
            text = _plain_html_text(bm.group(1), unescape)
            if text and text not in buttons:
                buttons.append(text)

        views.append(
            {
                "id": name,
                "titles": titles,
                "buttons": buttons,
                "body": body,
                "nameTokens": _name_tokens(name),
            }
        )
    return views


def _score_view_against_intake(view: dict[str, Any], intake_lower: str) -> int:
    """Higher = better match between a mockup view and Jira/prompt intent."""
    if not intake_lower:
        return 0
    score = 0
    for title in view.get("titles") or []:
        tl = title.lower()
        if tl and tl in intake_lower:
            score += 12
        for tok in re.findall(r"[a-z0-9]{3,}", tl):
            if tok in intake_lower:
                score += 2
    for tok in view.get("nameTokens") or []:
        if tok in intake_lower:
            score += 3
    # Generic body-copy overlap: reward distinctive words the view's OWN visible
    # copy (titles + buttons) shares with the intake text. No domain/product
    # keyword list — this generalizes to any mockup, any language.
    own_copy = " ".join((view.get("titles") or []) + (view.get("buttons") or [])).lower()
    for tok in set(re.findall(r"[a-z]{4,}", own_copy)):
        if tok in intake_lower:
            score += 1
    return score


def _select_primary_html_view(
    views: list[dict[str, Any]],
    intake_text: str,
) -> dict[str, Any] | None:
    """Pick one view when multi-screen HTML is clearly pointed at by intake text.

    Title/hero hits win first (longest matching title) so a ticket that names
    ``My datasets`` but also mentions console SLA details does not harvest the
    sibling console screen. Returns None when ambiguous — no guessing.
    """
    if len(views) < 2:
        return None
    intake_lower = re.sub(r"\s+", " ", (intake_text or "").lower()).strip()
    if len(intake_lower) < 8:
        return None

    title_hits: list[tuple[int, dict[str, Any], str]] = []
    for v in views:
        for title in v.get("titles") or []:
            tl = title.lower().strip()
            if len(tl) >= 4 and tl in intake_lower:
                title_hits.append((len(tl), v, title))
    if title_hits:
        title_hits.sort(key=lambda row: row[0], reverse=True)
        best_len, best_view, _ = title_hits[0]
        # Unique longest title match → lock that view.
        longer_or_equal = [h for h in title_hits if h[0] == best_len]
        distinct = {h[1]["id"] for h in longer_or_equal}
        if len(distinct) == 1:
            return best_view

    ranked = sorted(
        ((_score_view_against_intake(v, intake_lower), v) for v in views),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = ranked[0]
    second = ranked[1][0] if len(ranked) > 1 else 0
    if best_score < 8 or best_score < second + 3:
        return None
    return best


def _summarize_html_raw(raw: str, *, unescape: Any) -> dict[str, Any]:
    """Mechanical component/interaction extract from an HTML/JS-template blob."""
    components: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []

    def _plain(inner: str) -> str:
        return _plain_html_text(inner, unescape)

    for m in re.finditer(r"<h([1-6])[^>]*>(.*?)</h\1>", raw, re.I | re.S):
        text = _plain(m.group(2))
        if text:
            components.append({"type": f"Heading{m.group(1)}", "text": text, "confidence": 0.9})

    for m in _BUTTON_RE.finditer(raw):
        text = _plain(m.group(1))
        if text:
            components.append({"type": "Button", "label": text, "confidence": 0.85})
            interactions.append(
                {
                    "trigger": f"Click: {text}",
                    "expectedBehavior": "(from requirement)",
                    "confidence": 0.6,
                }
            )
    for m in re.finditer(
        r'<input[^>]+type=[\"\'](?:submit|button)[\"\'][^>]*value=[\"\']([^\"\']+)[\"\']',
        raw,
        re.I,
    ):
        text = unescape(m.group(1).strip())
        if text:
            components.append({"type": "Button", "label": text, "confidence": 0.8})

    for m in re.finditer(
        r"<(?:button|a|input|select|textarea|div|span)[^>]+"
        r"(?:aria-label|title)=[\"']([^\"']+)[\"']",
        raw,
        re.I,
    ):
        text = unescape(m.group(1).strip())
        if text:
            components.append({"type": "Label", "text": text, "confidence": 0.75})

    for m in re.finditer(r"<label\b[^>]*>(.*?)</label>", raw, re.I | re.S):
        text = _plain(m.group(1))
        if text:
            components.append({"type": "Label", "text": text, "confidence": 0.8})
    for m in re.finditer(r'<input[^>]+placeholder=[\"\']([^\"\']+)[\"\']', raw, re.I):
        ph = unescape(m.group(1).strip())
        if ph:
            components.append({"type": "Input", "placeholder": ph, "confidence": 0.8})

    for tag in ("nav", "header", "main", "footer", "aside", "section", "article"):
        if re.search(rf"<{tag}[\s>]", raw, re.I):
            regions.append({"id": tag, "type": tag, "confidence": 0.85})

    if re.search(r"<table[\s>]", raw, re.I):
        headers = re.findall(r"<th[^>]*>(.*?)</th>", raw, re.I | re.S)
        cols = [_plain(h) for h in headers if _plain(h)]
        components.append({"type": "Table", "columns": cols or [], "confidence": 0.85})

    for text in _extract_js_ui_strings(raw, unescape=unescape):
        components.append({"type": "Text", "text": text, "confidence": 0.7, "source": "js_ui_string"})

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for c in components:
        key = f"{c.get('type')}:{c.get('text') or c.get('label') or c.get('placeholder') or ''}"
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    return {
        "layout": {"regions": regions},
        "components": deduped,
        "interactions": interactions,
        "visualTokens": {"colors": [], "typography": [], "spacing": []},
        "uncertainFields": ["tokens", "exact_layout_bounds", "interactions"],
        "exactTextHints": harvest_exact_texts_from_components(deduped),
    }


def _extract_html_structure(
    html_path: str,
    *,
    run_dir: Path | None = None,
    intake_text: str = "",
) -> dict[str, Any]:
    """Mechanical DOM summary from a stored HTML file — no LLM.

    When the HTML mockup contains multiple ``function renderX()`` screens and
    intake text clearly names one (title / hero / tokens), components and
    exact-text hints are scoped to that view so sibling console/dashboard copy
    does not pollute the SoT. ``viewButtonMap`` always stays whole-file for
    cross-view leak checks.
    """
    import html as _html_mod

    from uiforgemax.pipeline.visual_sot import resolve_run_path

    path = resolve_run_path(run_dir, html_path) if run_dir else None
    if path is None:
        candidate = Path(html_path)
        path = candidate if candidate.is_absolute() and candidate.exists() else None
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore") if path else ""
    except OSError:
        raw = ""

    if not raw:
        return {
            "layout": {"regions": []},
            "components": [],
            "interactions": [],
            "visualTokens": {"colors": [], "typography": [], "spacing": []},
            "uncertainFields": ["html file not readable"],
        }

    unescape = _html_mod.unescape
    views = _extract_html_views(raw, unescape=unescape)
    view_button_map = {
        v["id"]: list(v.get("buttons") or [])
        for v in views
        if v.get("buttons")
    }
    primary = _select_primary_html_view(views, intake_text)
    source_raw = primary["body"] if primary else raw
    summary = _summarize_html_raw(source_raw, unescape=unescape)
    summary["viewButtonMap"] = view_button_map

    # Helper-call titles (e.g. hero('My datasets', 'Datasets you own.')) are JS
    # arguments, not DOM tags, so the mechanical tag harvest in _summarize_html_raw
    # misses them even though they are the screen's most important copy. Fold the
    # scoped view's titles (or every view's titles when unscoped) into
    # exactTextHints so the deterministic exact-copy gate actually covers hero /
    # heading text. Titles go first — highest-priority copy.
    title_pool = (
        list(primary.get("titles") or [])
        if primary
        else [t for v in views for t in (v.get("titles") or [])]
    )
    if title_pool:
        hints = list(summary.get("exactTextHints") or [])
        seen = {h.lower() for h in hints}
        merged: list[str] = []
        for t in title_pool:
            tl = t.lower()
            if tl not in seen and _looks_like_ui_copy(t):
                seen.add(tl)
                merged.append(t)
        summary["exactTextHints"] = (merged + hints)[:40]

    catalog = [
        {
            "id": v["id"],
            "titles": list(v.get("titles") or []),
            "buttonCount": len(v.get("buttons") or []),
        }
        for v in views
    ]
    if len(views) >= 2:
        summary["htmlMultiView"] = True
        summary["htmlViews"] = catalog
    if primary:
        summary["primaryHtmlView"] = primary["id"]
        summary["primaryHtmlViewTitles"] = list(primary.get("titles") or [])
        summary["outOfScopeHtmlViews"] = [c["id"] for c in catalog if c["id"] != primary["id"]]
        fields = list(summary.get("uncertainFields") or [])
        if "multi_view_html_scoped_by_intake" not in fields:
            fields.append("multi_view_html_scoped_by_intake")
        summary["uncertainFields"] = fields
    elif len(views) >= 2:
        summary["primaryHtmlView"] = None
        summary["htmlViewScopeAmbiguous"] = True

    return summary


_JS_UI_PROP_RE = re.compile(
    r"\b(?:label|title|heading|caption|placeholder|ariaLabel|buttonText|name|text)\s*[:=]\s*"
    r"[\"']([^\"'\n]{2,80})[\"']",
    re.I,
)
_JS_UI_CALL_RE = re.compile(
    r"\b(?:kpi|metric|stat|label|title|heading|caption|badge|tab|navItem)\s*\(\s*"
    r"[\"']([^\"'\n]{2,80})[\"']",
    re.I,
)
# Paths / URLs (case-insensitive). ALL_CAPS constants checked separately
# without IGNORECASE so short labels like "Bind" / "Live" are kept.
_JS_UI_PATH_RE = re.compile(
    r"^(https?://|/|\./|\.\./|[a-z]+://|[a-z_][a-z0-9_]*\.[a-z]{1,5})$",
    re.I,
)


def _looks_like_ui_copy(text: str) -> bool:
    t = text.strip()
    if len(t) < 2 or len(t) > 80:
        return False
    if not re.search(r"[A-Za-z]", t):
        return False
    if _JS_UI_PATH_RE.match(t):
        return False
    # SCREAMING_SNAKE / ALL_CAPS constants (not Title Case button labels).
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", t):
        return False
    if re.search(r"[{};=<>]|function\b|return\b", t):
        return False
    # Identifiers / code tokens (snake_case, camelCase) — not user-visible copy.
    if " " not in t and ("_" in t or re.fullmatch(r"[a-z]+(?:[A-Z][a-z0-9]*)+", t)):
        return False
    return True


def _extract_js_ui_strings(raw: str, *, unescape: Any) -> list[str]:
    """Pull short human-facing strings from JS/TS mockup helpers (generic).

    Covers ``title: "My datasets"`` and ``kpi("Pending governance", …)`` style
    copy inside HTML-as-JS SoT files — without scraping every string literal.
    """
    found: list[str] = []
    seen: set[str] = set()
    for rx in (_JS_UI_PROP_RE, _JS_UI_CALL_RE):
        for m in rx.finditer(raw):
            text = unescape(m.group(1).strip())
            key = text.lower()
            if key in seen or not _looks_like_ui_copy(text):
                continue
            seen.add(key)
            found.append(text)
    return found


def harvest_exact_texts_from_components(
    components: list[dict[str, Any]],
    *,
    limit: int = 40,
) -> list[str]:
    """Stable, de-duped exact-copy list from mechanical HTML extract components."""
    # Prefer interactive / heading labels; then general text / columns.
    priority = ("Button", "Heading1", "Heading2", "Heading3", "Label", "Input", "Text", "Table")
    buckets: dict[str, list[str]] = {k: [] for k in priority}
    other: list[str] = []
    seen: set[str] = set()

    def _add(bucket: str, text: str) -> None:
        t = re.sub(r"\s+", " ", (text or "").strip())
        if not t or not _looks_like_ui_copy(t):
            return
        key = t.lower()
        if key in seen:
            return
        seen.add(key)
        if bucket in buckets:
            buckets[bucket].append(t)
        else:
            other.append(t)

    for c in components:
        ctype = str(c.get("type") or "")
        if c.get("label"):
            _add(ctype if ctype in buckets else "Button", str(c["label"]))
        if c.get("text"):
            _add(ctype if ctype in buckets else "Text", str(c["text"]))
        if c.get("placeholder"):
            _add("Input", str(c["placeholder"]))
        for col in c.get("columns") or []:
            _add("Table", str(col))

    out: list[str] = []
    for key in priority:
        out.extend(buckets[key])
        if len(out) >= limit:
            return out[:limit]
    out.extend(other)
    return out[:limit]


def apply_html_sot_to_compliance(
    requirements: dict[str, Any],
    visual: dict[str, Any],
    *,
    sot: dict[str, Any] | None = None,
) -> None:
    """Merge HTML-derived exact copy into compliance when HTML is primary SoT.

    - Always merge ``exactTextHints`` / component labels into ``exactTextRequirements``
      (mediation may refine later; empty exact list was the main gap).
    - Set ``matchExactly=True`` only when HTML is the primary reference **and**
      we harvested enough concrete labels (≥3). Sparse HTML / prompt-only stays soft.
    """
    sot = sot or requirements.get("sourceOfTruth") or {}
    if not sot.get("primaryHtml") and not visual.get("htmlDerived"):
        return

    hints = list(visual.get("exactTextHints") or [])
    if not hints:
        hints = harvest_exact_texts_from_components(list(visual.get("components") or []))
    if not hints:
        return

    comp = requirements.setdefault("compliance", {})
    existing = [str(x) for x in (comp.get("exactTextRequirements") or []) if x]
    seen = {x.lower() for x in existing}
    for text in hints:
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        existing.append(text)
    comp["exactTextRequirements"] = existing[:40]
    comp.setdefault("referencePath", sot.get("primaryHtml") or visual.get("referenceHtml"))

    # Rich HTML SoT → require verbatim copy; leave Jira match-exactly as-is if already true.
    if sot.get("primaryHtml") and len(existing) >= 3:
        comp["matchExactly"] = True
        comp["htmlExactCopy"] = True
        note = (
            "HTML source-of-truth present — exactTextRequirements harvested mechanically "
            "from labels/headings/buttons (and JS UI helper strings). "
            "Match copy/structure; mediation may refine the list, not drop SoT labels."
        )
        assumptions = requirements.setdefault("assumptions", [])
        if note not in assumptions:
            assumptions.append(note)
        if visual.get("primaryHtmlView"):
            scope_note = (
                f"HTML multi-view SoT scoped to `{visual['primaryHtmlView']}` "
                f"(titles: {', '.join(visual.get('primaryHtmlViewTitles') or []) or 'n/a'}). "
                "Do not implement sibling screens listed in outOfScopeHtmlViews."
            )
            if scope_note not in assumptions:
                assumptions.append(scope_note)


def _extract_view_button_map(raw: str, *, unescape: Any) -> dict[str, list[str]]:
    """Map each top-level ``function renderX(){...}`` block to its own literal
    button labels — e.g. ``{"renderMyData": ["Bind"], "renderProducerConsole":
    ["Register a physical dataset", ...]}``.
    """
    return {
        v["id"]: list(v.get("buttons") or [])
        for v in _extract_html_views(raw, unescape=unescape)
        if v.get("buttons")
    }


def _extract_prompt_intent(prompt: str) -> dict[str, Any]:
    """Derive very rough visual intent from prompt text — structural hints only.

    No invented component names.  Returns the minimal scaffold needed so
    REQUIREMENT_ANALYSIS mediation has something to start from.
    """
    lower = prompt.lower()
    components: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []

    # Look for explicit UI nouns in the prompt
    _COMPONENT_HINTS = [
        (r"\btable\b|\bgrid\b|\blist\b|\bdatagrid\b", "Table/DataGrid"),
        (r"\bform\b|\bwizard\b", "Form"),
        (r"\bdashboard\b|\bchart\b|\bgraph\b|\bmetric\b", "Dashboard/Chart"),
        (r"\bmodal\b|\bdialog\b|\bdrawer\b", "Modal/Dialog"),
        (r"\bsidebar\b|\bnav\b|\bnavigation\b|\bmenu\b", "Navigation"),
        (r"\bsearch\b|\bfilter\b", "Search/Filter"),
        (r"\bbutton\b|\bcta\b", "Button"),
        (r"\bcard\b|\btile\b", "Card"),
        (r"\bmap\b", "Map"),
        (r"\bcalendar\b|\bdate\b", "Calendar/DatePicker"),
    ]
    for pattern, label in _COMPONENT_HINTS:
        if re.search(pattern, lower):
            components.append({"type": label, "promptDerived": True, "confidence": 0.5})

    # Look for action verbs → interactions
    _ACTION_HINTS = [
        (r"\bexport\b", "Export action"),
        (r"\bimport\b|\bupload\b", "Import/Upload action"),
        (r"\bdelete\b|\bremove\b", "Delete action"),
        (r"\bedit\b|\bupdate\b|\bmodify\b", "Edit/Update action"),
        (r"\badd\b|\bcreate\b|\bnew\b", "Create/Add action"),
        (r"\bsearch\b|\bquery\b", "Search action"),
    ]
    for pattern, label in _ACTION_HINTS:
        if re.search(pattern, lower):
            interactions.append({"trigger": label, "promptDerived": True, "confidence": 0.5})

    return {
        "layout": {"regions": []},
        "components": components,
        "interactions": interactions,
        "visualTokens": {"colors": [], "typography": [], "spacing": []},
        "uncertainFields": ["all — refine via REQUIREMENT_ANALYSIS mediation"],
    }


def build_from_jira(
    jira_raw: dict[str, Any],
    attachment: dict[str, Any] | None,
    source_of_truth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build draft requirements from the Jira issue itself — never a demo template.

    Scope/dataNeeds stay minimal; IDE REQUIREMENT_ANALYSIS mediation fills detail.
    """
    overrides = parse_jira_overrides(jira_raw.get("comments", []))
    match_exactly = jira_raw.get("customFields", {}).get("matchExactly") or "match-exactly" in jira_raw.get(
        "labels", []
    )
    description = (jira_raw.get("description") or "").strip()
    summary = (jira_raw.get("summary") or "").strip() or "Jira issue"
    # Prefer real ticket text for in-scope; avoid hardcoding customer-list demo scope.
    scope_in = [description] if description else [summary]
    acs = jira_raw.get("acceptanceCriteria") or []
    if not acs and description:
        acs = [{"id": "AC-1", "text": description}]
    exact = []
    if match_exactly and attachment:
        # Only invent exact-text when matchExactly is set; never default Export CSV.
        exact = []
    sot = source_of_truth or {}
    primary = (
        sot.get("primaryHtml")
        or sot.get("primaryImage")
        or ((attachment or {}).get("storedPath") if attachment else None)
        or ((attachment or {}).get("filename") if attachment else None)
    )
    return {
        "issueKey": jira_raw.get("key"),
        "summary": summary,
        "scope": {
            "in": scope_in,
            "out": [],
        },
        "acceptanceCriteria": acs,
        "compliance": {
            "matchExactly": bool(match_exactly),
            "referenceAttachment": (attachment or {}).get("filename"),
            "referencePath": primary,
            "exactTextRequirements": exact,
        },
        "sourceOfTruth": sot,
        "policy": {
            "resolutionPolicy": jira_raw.get("customFields", {}).get("resolutionPolicy") or "pending",
            "targetApp": jira_raw.get("customFields", {}).get("targetApp") or "pending",
            "targetDomain": jira_raw.get("customFields", {}).get("targetDomain") or "pending",
        },
        "overrides": overrides,
        "dataNeeds": [],
        "assumptions": [
            "Draft scope taken from Jira summary/description only — refine in REQUIREMENT_ANALYSIS mediation."
        ],
        "conflicts": [],
    }


def build_from_prompt(
    prompt: str,
    policy: str,
    classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build draft requirements from a free-text prompt.

    ``classification`` is the request-classification.json produced by
    REQUEST_CLASSIFICATION mediation (or the heuristic default).  We read
    ``targets.apps`` / ``targets.domains`` from it rather than hardcoding any
    demo domain names.  All AC / dataNeeds are left sparse so
    REQUIREMENT_ANALYSIS mediation can fill them from the actual prompt.
    """
    classification = classification or {}
    targets = classification.get("targets") or {}
    # Use whatever the IDE model or heuristic classified — never invent a
    # specific app/domain name that isn't in the prompt or classification.
    apps = targets.get("apps") or []
    domains = targets.get("domains") or []
    target_app = apps[0] if apps else "app"
    target_domain = domains[0] if domains else "product"

    # Do not invent dataNeeds — leave empty so REQUIREMENT_ANALYSIS mediation
    # derives them from the actual prompt text.
    return {
        "issueKey": "PROMPT",
        "summary": prompt[:120],
        "scope": {"in": [prompt], "out": []},
        "acceptanceCriteria": [{"id": "AC-1", "text": prompt}],
        "compliance": {"matchExactly": False, "referenceAttachment": None, "exactTextRequirements": []},
        "policy": {
            "resolutionPolicy": policy,
            "targetApp": target_app,
            "targetDomain": target_domain,
        },
        "overrides": [],
        "dataNeeds": [],  # REQUIREMENT_ANALYSIS mediation fills this from prompt
        "assumptions": [
            "Draft requirements from prompt — refine in REQUIREMENT_ANALYSIS mediation."
        ],
        "conflicts": [],
    }


def apply_overrides(requirements: dict[str, Any]) -> None:
    """Apply Jira comment overrides to the requirements in-place.

    Processed chronologically (``overrides[]`` is already in comment order).
    The latest override for any given concern wins.  We record each applied
    override in ``assumptions[]`` so Gate 2/3 surfaces them to the human.
    """
    scope_out: list[str] = list(requirements.get("scope", {}).get("out") or [])

    for override in requirements.get("overrides", []):
        if not override.get("isOverride"):
            continue
        body = override.get("body", "")
        author = override.get("author") or "commenter"
        created = override.get("created") or ""

        # Theme / colour overrides
        color_m = re.search(r"use\s+(green|blue|red|purple|orange|teal|dark|light)\s+(?:primary\s+)?(?:theme|color|colour)", body, re.I)
        if color_m:
            theme = color_m.group(1).lower()
            note = f"Override ({author} {created}): use '{theme}' as primary theme/color."
            if note not in requirements["assumptions"]:
                requirements["assumptions"].append(note)

        # Scope-out overrides: "don't create X", "do not create X", "remove X"
        for pat in (
            re.compile(r"don'?t\s+create\s+(.+)", re.I),
            re.compile(r"do\s+not\s+create\s+(.+)", re.I),
            re.compile(r"remove\s+(.+)", re.I),
            re.compile(r"skip\s+(.+)", re.I),
        ):
            m = pat.search(body)
            if m:
                item = m.group(1).strip().rstrip(".")
                if item and item not in scope_out:
                    scope_out.append(item)
                    note = f"Override ({author} {created}): '{item}' excluded from scope."
                    if note not in requirements["assumptions"]:
                        requirements["assumptions"].append(note)

        # Change overrides: "change X to Y"
        change_m = re.search(r"change\s+(.+?)\s+to\s+(.+)", body, re.I)
        if change_m:
            from_val, to_val = change_m.group(1).strip(), change_m.group(2).strip().rstrip(".")
            note = f"Override ({author} {created}): change '{from_val}' to '{to_val}'."
            if note not in requirements["assumptions"]:
                requirements["assumptions"].append(note)

        # Generic "don't use X" / "override: …"
        dont_use_m = re.search(r"don'?t\s+use\s+(.+)", body, re.I)
        if dont_use_m:
            item = dont_use_m.group(1).strip().rstrip(".")
            note = f"Override ({author} {created}): do not use '{item}'."
            if note not in requirements["assumptions"]:
                requirements["assumptions"].append(note)

        explicit_m = re.search(r"override:\s*(.+)", body, re.I)
        if explicit_m:
            item = explicit_m.group(1).strip()
            note = f"Override ({author} {created}): {item}"
            if note not in requirements["assumptions"]:
                requirements["assumptions"].append(note)

    requirements.setdefault("scope", {})["out"] = scope_out


def normalize_run(run_dir: Path, policy: str, feedback: str | None = None) -> tuple[dict, dict]:
    """Build normalized requirements + visual-spec from all available intake inputs.

    Priority order for targetApp / targetDomain:
      1. Request classification (IDE model or heuristic) → ``targets.apps/domains``
      2. Jira custom fields ``targetApp`` / ``targetDomain``
      3. Generic fallback ``"app"`` / ``"product"`` (never a hardcoded demo name)

    Priority order for visual spec:
      1. Image present → provisional shell; VISUAL_INTERPRETATION mediation required
      2. HTML present → mechanical DOM extract (no LLM)
      3. Prompt only  → minimal intent-derived skeleton
    """
    jira_path = run_dir / "inputs" / "jira.raw.json"
    prompt_path = run_dir / "inputs" / "prompt.txt"
    cls_path = run_dir / "request-classification.json"
    classification: dict = {}
    if cls_path.exists():
        try:
            classification = json.loads(cls_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            classification = {}

    greenfield = is_greenfield(classification)
    sot = collect_source_of_truth(run_dir)

    # Determine whether images are present in this run.
    has_image = bool(sot.get("primaryImage") or sot.get("images"))

    if greenfield:
        from uiforgemax.pipeline.greenfield import build_greenfield_requirements

        requirements = build_greenfield_requirements(run_dir, policy, classification)
    elif jira_path.exists():
        jira_raw = json.loads(jira_path.read_text(encoding="utf-8"))
        attachment = (jira_raw.get("attachments") or [None])[0]
        requirements = build_from_jira(jira_raw, attachment, sot)
        # Honour classification targets even for Jira runs (IDE model may have
        # refined them from prompt + image context).
        targets = classification.get("targets") or {}
        if targets.get("apps"):
            requirements.setdefault("policy", {})["targetApp"] = targets["apps"][0]
        if targets.get("domains"):
            requirements.setdefault("policy", {})["targetDomain"] = targets["domains"][0]
    elif prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8")
        # Pass classification so build_from_prompt reads targets, not hardcoded names.
        requirements = build_from_prompt(prompt, policy, classification=classification)
        if feedback:
            requirements["assumptions"].append(f"User feedback: {feedback}")
    else:
        requirements = build_from_prompt(
            "Implement requested feature", policy, classification=classification
        )

    # Always attach the run SoT catalog (HTML / wireframe / design notes).
    requirements["sourceOfTruth"] = sot
    if sot.get("primaryHtml") or sot.get("primaryImage"):
        requirements.setdefault("compliance", {})["referencePath"] = (
            sot.get("primaryHtml") or sot.get("primaryImage")
        )

    # Prefer the run policy chosen at classify (or explicit start_run) over any
    # leftover "pending" placeholder from intake.
    if policy and policy != "pending":
        requirements.setdefault("policy", {})["resolutionPolicy"] = policy

    # ui_only tickets must not carry stale API/entity dataNeeds into later stages.
    if classification.get("surface") == "ui_only":
        requirements["dataNeeds"] = []

    # Apply Jira comment overrides (chronological, latest wins).
    apply_overrides(requirements)
    if feedback and jira_path.exists():
        requirements["assumptions"].append(f"User feedback: {feedback}")

    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else None
    flow = classification.get("surface")
    skip_visual = (
        flow == "api_only"
        and not has_image
        and not sot.get("primaryHtml")
    )

    visual: dict = {}
    if not skip_visual and not greenfield:
        # visual_name is the primary SoT image path (if any), else a generic label.
        visual_name = sot.get("primaryImage") or sot.get("primaryHtml") or "prompt"
        intake_blob = " ".join(
            [
                str(requirements.get("summary") or ""),
                " ".join(str(x) for x in (requirements.get("scope") or {}).get("in") or []),
                " ".join(
                    str(ac.get("text") or "")
                    for ac in (requirements.get("acceptanceCriteria") or [])
                    if isinstance(ac, dict)
                ),
                prompt_text or "",
            ]
        )
        visual = build_visual_spec(
            visual_name,
            prompt=prompt_text,
            sot=sot,
            has_image=has_image,
            run_dir=run_dir,
            intake_text=intake_blob,
        )
        # Image path returns a mediation shell and skips DOM extract — still harvest
        # exact copy from page.html when HTML SoT is present (any intake combo).
        if sot.get("primaryHtml") and not visual.get("exactTextHints"):
            html_bits = _extract_html_structure(
                str(sot["primaryHtml"]), run_dir=run_dir, intake_text=intake_blob
            )
            visual["exactTextHints"] = list(html_bits.get("exactTextHints") or [])
            for key in (
                "viewButtonMap",
                "primaryHtmlView",
                "primaryHtmlViewTitles",
                "outOfScopeHtmlViews",
                "htmlMultiView",
                "htmlViews",
                "htmlViewScopeAmbiguous",
            ):
                if html_bits.get(key) is not None and not visual.get(key):
                    visual[key] = html_bits[key]
            if not visual.get("htmlDerived"):
                visual["htmlExactTextSource"] = sot.get("primaryHtml")
        apply_html_sot_to_compliance(requirements, visual, sot=sot)
        (run_dir / "visual-spec.json").write_text(json.dumps(visual, indent=2), encoding="utf-8")
    elif not skip_visual and greenfield:
        visual = {
            "source": sot.get("primaryHtml") or sot.get("primaryImage") or ("prompt" if prompt_text else "greenfield"),
            "summary": (prompt_text or requirements.get("summary") or "")[:200],
            "greenfield": True,
            "visualSpecUnconfirmed": has_image,  # needs mediation if image present
            "sourceOfTruth": sot,
            "referenceHtml": sot.get("primaryHtml"),
            "referenceImage": sot.get("primaryImage"),
            "referenceArtifacts": sot.get("attachments") or [],
            "designNotes": sot.get("designNotes") or [],
        }
        (run_dir / "visual-spec.json").write_text(json.dumps(visual, indent=2), encoding="utf-8")

    (run_dir / "requirements.normalized.json").write_text(
        json.dumps(requirements, indent=2), encoding="utf-8"
    )
    return visual, requirements
