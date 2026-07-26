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
        html_summary = _extract_html_structure(primary_html, run_dir=run_dir)
        return {
            "source": primary_html,
            "confidence": 0.6,
            "visualSpecUnconfirmed": False,
            "htmlDerived": True,
            "note": (
                "Derived mechanically from inputs/page.html. VISUAL_INTERPRETATION mediation "
                "refines component list, shell/sidebar geometry, tokens, and interactions "
                "against the HTML source of truth."
            ),
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


def _extract_html_structure(
    html_path: str,
    *,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Mechanical DOM summary from a stored HTML file — no LLM.

    Extracts headings, button labels, form field labels, link text, and
    top-level landmark regions as structural hints for mediation.  Never
    invents data that is not present in the file.

    ``html_path`` may be run-relative (``inputs/page.html``); resolve against
    ``run_dir`` when provided so MCP CWD cannot miss the file.
    """
    import html as _html_mod

    from uiforgemax.pipeline.visual_sot import resolve_run_path

    components: list[dict[str, Any]] = []
    interactions: list[dict[str, Any]] = []
    regions: list[dict[str, Any]] = []

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

    # Headings
    for m in re.finditer(r"<h([1-6])[^>]*>([^<]+)", raw, re.I):
        level, text = m.group(1), _html_mod.unescape(m.group(2).strip())
        if text:
            components.append({"type": f"Heading{level}", "text": text, "confidence": 0.9})

    # Buttons (button tags + input[type=submit/button] + role=button)
    for m in re.finditer(r"<button[^>]*>([^<]+)<", raw, re.I):
        text = _html_mod.unescape(m.group(1).strip())
        if text:
            components.append({"type": "Button", "label": text, "confidence": 0.85})
            interactions.append({"trigger": f"Click: {text}", "expectedBehavior": "(from requirement)", "confidence": 0.6})
    # input[type=submit/button] with value attribute
    for m in re.finditer(r'<input[^>]+type=[\"\'](?:submit|button)[\"\'][^>]*value=[\"\']([^\"\']+)[\"\']', raw, re.I):
        text = _html_mod.unescape(m.group(1).strip())
        if text:
            components.append({"type": "Button", "label": text, "confidence": 0.8})

    # Form inputs / labels
    for m in re.finditer(r"<label[^>]*>([^<]+)<", raw, re.I):
        text = _html_mod.unescape(m.group(1).strip())
        if text:
            components.append({"type": "Label", "text": text, "confidence": 0.8})
    # input placeholder
    for m in re.finditer(r'<input[^>]+placeholder=[\"\']([^\"\']+)[\"\']', raw, re.I):
        ph = _html_mod.unescape(m.group(1).strip())
        if ph:
            components.append({"type": "Input", "placeholder": ph, "confidence": 0.8})

    # Landmark regions
    for tag in ("nav", "header", "main", "footer", "aside", "section", "article"):
        if re.search(rf"<{tag}[\s>]", raw, re.I):
            regions.append({"id": tag, "type": tag, "confidence": 0.85})

    # Tables → DataGrid hint
    if re.search(r"<table[\s>]", raw, re.I):
        headers = re.findall(r"<th[^>]*>([^<]+)", raw, re.I)
        cols = [_html_mod.unescape(h.strip()) for h in headers if h.strip()]
        components.append({"type": "Table", "columns": cols or [], "confidence": 0.85})

    # Deduplicate by (type, primary text field)
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
        visual = build_visual_spec(
            visual_name,
            prompt=prompt_text,
            sot=sot,
            has_image=has_image,
            run_dir=run_dir,
        )
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
