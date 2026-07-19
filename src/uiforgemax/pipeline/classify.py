"""Request classification — deterministic signal-gathering + heuristic default.

The MCP never *decides* the request type; it collects observable signals from the
intake (input modes, images + roles, Jira/prompt/HTML text, repo state) and writes
a best-guess default. The IDE model then confirms/overrides via the
``REQUEST_CLASSIFICATION`` mediation, and that decision drives the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_KEYWORDS: dict[str, list[str]] = {
    "openfin": ["openfin"],
    "electron": ["electron"],
    "desktop": ["desktop app", "desktop application"],
    "mobile": ["mobile app", "react native", "android", "ios"],
    "greenfield": ["greenfield", "from scratch", "new app", "new application", "brand new", "scaffold"],
    "api": ["api", "endpoint", "rest", "graphql", "backend", "service", "contract"],
    "migration": ["migrate", "migration", "port to", "upgrade"],
    "refactor": ["refactor", "clean up", "restructure"],
    "bugfix": ["bug", "fix", "regression", "broken", "defect"],
}


def _image_roles(run_dir: Path) -> list[dict[str, str]]:
    manifest = run_dir / "inputs" / "images.json"
    exts = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}
    files = []
    inputs = run_dir / "inputs"
    if inputs.exists():
        files = sorted(
            str(p.relative_to(run_dir)).replace("\\", "/")
            for p in inputs.iterdir()
            if p.suffix.lower() in exts
        )
    roles: dict[str, str] = {}
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.get("images", []):
                path = str(entry.get("path", "")).replace("\\", "/")
                if path:
                    roles[path] = entry.get("role", "reference")
        except (json.JSONDecodeError, OSError):
            pass
    return [{"path": p, "role": roles.get(p, "reference")} for p in files]


def _intake_text(run_dir: Path) -> str:
    parts: list[str] = []
    prompt = run_dir / "inputs" / "prompt.txt"
    if prompt.exists():
        parts.append(prompt.read_text(encoding="utf-8", errors="ignore"))
    jira = run_dir / "inputs" / "jira.raw.json"
    if jira.exists():
        try:
            raw = json.loads(jira.read_text(encoding="utf-8"))
            parts.append(raw.get("summary", ""))
            parts.append(raw.get("description", "") or "")
            parts.extend(raw.get("labels", []) or [])
            for c in raw.get("comments", []) or []:
                parts.append(c.get("body", ""))
        except (json.JSONDecodeError, OSError):
            pass
    return "\n".join(p for p in parts if p).lower()


def _matched_keywords(text: str) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for key, terms in _KEYWORDS.items():
        found = [t for t in terms if t in text]
        if found:
            hits[key] = found
    return hits


def is_greenfield(classification: dict[str, Any]) -> bool:
    """Single canonical predicate for "is this a greenfield scaffold?".

    ``greenfieldScaffold`` is the authoritative flag (set by the heuristic
    default and preserved/overridden by IDE mediation). Fall back to
    ``requestType`` only when the flag was never set at all.
    """
    flag = classification.get("greenfieldScaffold")
    if flag is not None:
        return bool(flag)
    return classification.get("requestType") == "greenfield"


def build_classification_signals(state: Any, run_dir: Path) -> dict[str, Any]:
    modes = list(state.inputs.get("modes", []))
    images = _image_roles(run_dir)
    roles = {img["role"] for img in images}
    text = _intake_text(run_dir)
    keywords = _matched_keywords(text)

    project_root = state.project_root
    root_path = Path(project_root) if project_root else None
    root_empty = bool(root_path and root_path.exists() and not any(root_path.iterdir()))

    return {
        "inputModes": modes,
        "images": images,
        "hasBeforeAfter": "before" in roles and "after" in roles,
        "imageCount": len(images),
        "keywords": keywords,
        "projectRoot": {
            "set": bool(project_root),
            "exists": bool(root_path and root_path.exists()),
            "empty": root_empty,
        },
        "architecture": dict(state.architecture or {}),
        "textExcerpt": text[:600],
    }


def default_classification(signals: dict[str, Any]) -> dict[str, Any]:
    """Heuristic best-guess used before/without IDE mediation (e.g. in tests)."""
    kw = signals.get("keywords", {})
    modes = signals.get("inputModes", [])
    root = signals.get("projectRoot", {})

    platform = ["web"]
    for p in ("openfin", "electron", "desktop", "mobile"):
        if p in kw:
            platform = [p]
            break

    if kw.get("greenfield") or (root.get("set") and root.get("empty")):
        request_type = "greenfield"
    elif kw.get("migration"):
        request_type = "migration"
    elif kw.get("refactor"):
        request_type = "refactor"
    elif kw.get("bugfix"):
        request_type = "bugfix"
    else:
        request_type = "enhancement"

    has_ui = ("image" in modes) or ("html" in modes)
    has_api = bool(kw.get("api"))
    if request_type == "greenfield":
        surface = "full_stack"
    elif has_ui and has_api:
        surface = "full_stack"
    elif has_api and not has_ui:
        surface = "api_only"
    elif has_ui:
        surface = "ui_only"
    else:
        surface = "unknown"

    change_signal: list[str] = []
    if signals.get("hasBeforeAfter"):
        change_signal.append("before_after_screens")
    elif signals.get("imageCount"):
        change_signal.append("single_wireframe")
    if "jira" in modes:
        change_signal.append("jira_spec")
    if "prompt" in modes:
        change_signal.append("prompt_only")

    policy_by_surface = {
        "ui_only": "frontend_first",
        "api_only": "contract_driven",
        "full_stack": "full_stack",
    }
    # Heuristic only — not a global config default. Unknown surface keeps
    # frontend_first so missing APIs can mock rather than hard-block Gate 1.
    recommended_policy = policy_by_surface.get(surface, "frontend_first")
    use_graph = request_type != "greenfield" and not root.get("empty")

    return {
        "requestType": request_type,
        "surface": surface,
        "platform": platform,
        "changeSignal": change_signal or ["prompt_only"],
        "targets": {"apps": [], "domains": []},
        "capabilities": [],
        "recommendedPolicy": recommended_policy,
        "useGraph": use_graph,
        "runApi": surface != "ui_only",
        "runVisual": surface != "api_only" or bool(signals.get("imageCount")),
        "greenfieldScaffold": request_type == "greenfield" or root.get("empty"),
        "rationale": "Deterministic heuristic default (pre-mediation).",
        "confidence": 0.4,
        "needsHumanConfirmation": surface == "unknown" or request_type == "greenfield",
        "source": "heuristic_default",
    }
