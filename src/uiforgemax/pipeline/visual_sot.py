"""Detect visual source-of-truth from any intake: HTML, images, wireframes, mockups.

Used by flow routing, visual validation, and mediation so fidelity checks are not
image-only. HTML / wireframe / mockup attachments count the same as screenshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.source_of_truth import collect_source_of_truth

_VISUAL_ROLES = frozenset({"html", "image", "wireframe", "mockup", "before", "after"})


def resolve_run_path(run_dir: Path, rel_or_abs: str | None) -> Path | None:
    """Resolve a run-relative path (e.g. inputs/page.html) against ``run_dir``."""
    if not rel_or_abs:
        return None
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p if p.exists() else None
    candidate = run_dir / rel_or_abs
    return candidate if candidate.exists() else None


def detect_visual_references(run_dir: Path, state: Any | None = None) -> dict[str, Any]:
    """Summarize every visual SoT available for this run.

    Returns keys used by flow/mediation:
    - hasVisualRef: any HTML / image / wireframe / mockup / design visual
    - hasImages: binary image inputs (for IMAGE_CONVERT / readImages)
    - hasHtml: page.html or HTML attachment
    - hasWireframeOrMockup: role-based attachments
    - kinds: list of detected kinds
    - primaryHtml / primaryImage / artifacts
    """
    sot = collect_source_of_truth(run_dir)
    modes: list[str] = []
    if state is not None:
        inputs = getattr(state, "inputs", None) or {}
        if isinstance(inputs, dict):
            modes = list(inputs.get("modes") or [])

    primary_html = sot.get("primaryHtml")
    primary_image = sot.get("primaryImage")
    html_path = resolve_run_path(run_dir, primary_html)
    image_path = resolve_run_path(run_dir, primary_image)

    attachments = list(sot.get("attachments") or [])
    roles = {str(a.get("role") or "") for a in attachments if a.get("path")}
    visual_atts = [
        a
        for a in attachments
        if (a.get("role") in _VISUAL_ROLES or a.get("role") == "design_notes") and a.get("path")
    ]

    images_manifest: list[dict[str, Any]] = list(sot.get("images") or [])
    visual_spec_images = False
    visual_path = run_dir / "visual-spec.json"
    if visual_path.exists():
        try:
            vs = json.loads(visual_path.read_text(encoding="utf-8"))
            visual_spec_images = bool(
                vs.get("images") or vs.get("wireframes") or vs.get("mockups") or vs.get("htmlDerived")
            )
        except (OSError, json.JSONDecodeError):
            pass

    has_html = bool(html_path) or "html" in modes or "html" in roles
    has_images = (
        bool(image_path)
        or bool(images_manifest)
        or "image" in modes
        or bool(roles & {"image", "wireframe", "mockup", "before", "after"})
    )
    has_wire = bool(roles & {"wireframe", "mockup"}) or any(
        (a.get("role") in {"wireframe", "mockup"}) for a in visual_atts
    )
    # Design notes may appear as sot.designNotes[] (from write_attachments_manifest)
    # OR only as attachment role=design_notes without a top-level designNotes key
    # (common for hand-written / partial attachments.json).
    design_notes = list(sot.get("designNotes") or [])
    if not design_notes:
        design_notes = [
            str(a.get("path"))
            for a in attachments
            if a.get("role") == "design_notes" and a.get("path")
        ]
    has_design_notes = bool(design_notes) or "design_notes" in roles
    # Design notes describe intent (exact copy/labels/layout) as concretely as a
    # wireframe — count them toward hasVisualRef so kinds[] and flow stay consistent.
    has_visual = has_html or has_images or has_wire or visual_spec_images or has_design_notes

    kinds: list[str] = []
    if has_html:
        kinds.append("html")
    if has_images:
        kinds.append("image")
    if has_wire:
        kinds.append("wireframe_or_mockup")
    if has_design_notes:
        kinds.append("design_notes")

    return {
        "hasVisualRef": has_visual,
        "hasImages": has_images,
        "hasHtml": has_html,
        "hasWireframeOrMockup": has_wire,
        "kinds": kinds,
        "primaryHtml": "inputs/page.html" if html_path and html_path.name == "page.html" else primary_html,
        "primaryImage": primary_image,
        "htmlResolved": str(html_path) if html_path else None,
        "imageResolved": str(image_path) if image_path else None,
        "artifacts": [
            {"path": a.get("path"), "role": a.get("role"), "filename": a.get("filename")}
            for a in visual_atts
        ],
        "designNotes": design_notes,
        "modes": modes,
    }
