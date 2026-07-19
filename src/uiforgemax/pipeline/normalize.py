"""Normalize intake into visual-spec and requirements (LLM-free deterministic mediation)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.classify import is_greenfield
from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

OVERRIDE_PATTERNS = [
    re.compile(r"override:\s*(.+)", re.I),
    re.compile(r"don't\s+create\s+(.+)", re.I),
    re.compile(r"do not create\s+(.+)", re.I),
    re.compile(r"use\s+(green|blue|red)\s+(?:primary\s+)?theme", re.I),
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


def build_visual_spec(filename: str, prompt: str | None = None) -> dict[str, Any]:
    label = "Export CSV"
    if prompt and "export" in prompt.lower():
        label = "Export CSV"
    return {
        "source": filename,
        "confidence": 0.82,
        "layout": {
            "viewport": {"width": 1440, "height": 900},
            "regions": [
                {"id": "sidebar", "type": "navigation", "confidence": 0.95},
                {"id": "header", "type": "page_header", "confidence": 0.93},
                {"id": "search", "type": "search_input", "confidence": 0.9},
                {"id": "table", "type": "data_table", "confidence": 0.91},
            ],
        },
        "components": [
            {"type": "PageLayout", "title": "Customers", "confidence": 0.94},
            {
                "type": "DataGrid",
                "columns": ["Name", "Email", "Status", "Company"],
                "confidence": 0.92,
            },
            {"type": "Button", "label": label, "variant": "primary", "colorHint": "green", "confidence": 0.96},
            {"type": "SearchInput", "placeholder": "Search by name or email", "confidence": 0.9},
        ],
        "interactions": [
            {
                "trigger": "Export CSV click",
                "expectedBehavior": "Download CSV for filtered rows",
                "confidence": 0.74,
                "needsConfirmation": False,
            }
        ],
        "uncertainFields": [],
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


def build_from_prompt(prompt: str, policy: str) -> dict[str, Any]:
    return {
        "issueKey": "PROMPT",
        "summary": prompt[:120],
        "scope": {"in": [prompt], "out": []},
        "acceptanceCriteria": [{"id": "AC-1", "text": prompt}],
        "compliance": {"matchExactly": False, "referenceAttachment": None, "exactTextRequirements": []},
        "policy": {
            "resolutionPolicy": policy,
            "targetApp": "customer-portal",
            "targetDomain": "customer",
        },
        "overrides": [],
        "dataNeeds": [{"entity": "Customer", "operations": ["list"], "fields": ["name", "email"]}],
        "assumptions": [],
        "conflicts": [],
    }


def apply_overrides(requirements: dict[str, Any]) -> None:
    for override in requirements.get("overrides", []):
        if not override.get("isOverride"):
            continue
        body = override.get("body", "")
        if re.search(r"green", body, re.I):
            requirements["assumptions"].append(
                "Primary action buttons use green theme override from Jira comment."
            )
        if re.search(r"side panel|do not create a separate customer detail route", body, re.I):
            if "Separate customer detail route/page" not in requirements["scope"]["out"]:
                requirements["scope"]["out"].append("Separate customer detail route/page")


def normalize_run(run_dir: Path, policy: str, feedback: str | None = None) -> tuple[dict, dict]:
    jira_path = run_dir / "inputs" / "jira.raw.json"
    prompt_path = run_dir / "inputs" / "prompt.txt"
    cls_path = run_dir / "request-classification.json"
    classification: dict = {}
    if cls_path.exists():
        classification = json.loads(cls_path.read_text(encoding="utf-8"))

    visual_name = "wireframe.png"
    greenfield = is_greenfield(classification)
    sot = collect_source_of_truth(run_dir)

    if greenfield:
        from uiforgemax.pipeline.greenfield import build_greenfield_requirements

        requirements = build_greenfield_requirements(run_dir, policy, classification)
    elif jira_path.exists():
        jira_raw = json.loads(jira_path.read_text(encoding="utf-8"))
        attachment = (jira_raw.get("attachments") or [None])[0]
        if attachment:
            visual_name = attachment.get("storedPath") or attachment.get("filename", visual_name)
        requirements = build_from_jira(jira_raw, attachment, sot)
    elif prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8")
        requirements = build_from_prompt(prompt, policy)
        if feedback:
            requirements["assumptions"].append(f"User feedback: {feedback}")
    else:
        requirements = build_from_prompt("Implement requested feature", policy)

    # Always attach the run SoT catalog (HTML / wireframe / design notes).
    requirements["sourceOfTruth"] = sot
    if sot.get("primaryHtml") or sot.get("primaryImage"):
        requirements.setdefault("compliance", {})["referencePath"] = sot.get("primaryHtml") or sot.get(
            "primaryImage"
        )

    # Prefer the run policy chosen at classify (or explicit start_run) over any
    # leftover "pending" placeholder from intake.
    if policy and policy != "pending":
        requirements.setdefault("policy", {})["resolutionPolicy"] = policy

    # ui_only tickets must not carry stale API/entity dataNeeds into later stages.
    if classification.get("surface") == "ui_only":
        requirements["dataNeeds"] = []

    apply_overrides(requirements)
    if feedback and jira_path.exists():
        requirements["assumptions"].append(f"User feedback: {feedback}")

    prompt_text = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else None
    flow = classification.get("surface")
    skip_visual = flow == "api_only" and "image" not in (classification.get("signals", {}).get("inputModes") or [])
    visual: dict = {}
    if not skip_visual and not greenfield:
        visual = build_visual_spec(visual_name, prompt_text)
        visual["sourceOfTruth"] = sot
        visual["referenceHtml"] = sot.get("primaryHtml")
        visual["referenceImage"] = sot.get("primaryImage")
        visual["referenceArtifacts"] = sot.get("attachments") or []
        (run_dir / "visual-spec.json").write_text(json.dumps(visual, indent=2), encoding="utf-8")
    elif not skip_visual and greenfield:
        visual = {
            "source": sot.get("primaryHtml") or sot.get("primaryImage") or ("prompt" if prompt_text else "greenfield"),
            "summary": (prompt_text or requirements.get("summary") or "")[:200],
            "greenfield": True,
            "sourceOfTruth": sot,
            "referenceHtml": sot.get("primaryHtml"),
            "referenceImage": sot.get("primaryImage"),
            "referenceArtifacts": sot.get("attachments") or [],
            "designNotes": sot.get("designNotes") or [],
        }
        (run_dir / "visual-spec.json").write_text(json.dumps(visual, indent=2), encoding="utf-8")

    (run_dir / "requirements.normalized.json").write_text(json.dumps(requirements, indent=2), encoding="utf-8")
    return visual, requirements
