"""Source-of-truth intake artifacts (Jira attachments, HTML, images, design notes).

Stored under the run temp dir (`inputs/attachments/`, `inputs/page.html`, …) and
referenced by normalize / plan / mediation so visual HTML/wireframes are never ignored.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".bmp"}
_HTML_EXTS = {".html", ".htm"}
_NOTES_EXTS = {".md", ".markdown", ".txt", ".pdf"}


def infer_attachment_role(filename: str, mime_type: str | None = None) -> str:
    """Classify an attachment for plan / mediation references."""
    name = (filename or "").lower()
    mime = (mime_type or "").lower()
    stem = Path(name).stem
    if any(k in stem for k in ("wireframe", "wire-frame")):
        return "wireframe"
    if "mockup" in stem or "mock-up" in stem:
        return "mockup"
    if re.search(r"(^|[_-])before([_-]|$)", stem):
        return "before"
    if re.search(r"(^|[_-])after([_-]|$)", stem):
        return "after"
    if Path(name).suffix.lower() in _HTML_EXTS or "html" in mime:
        return "html"
    if Path(name).suffix.lower() in _IMAGE_EXTS or mime.startswith("image/"):
        return "image"
    if any(k in stem for k in ("production_plan", "design", "token", "theme", "notes")):
        return "design_notes"
    if Path(name).suffix.lower() in _NOTES_EXTS or mime.startswith("text/"):
        return "design_notes"
    return "document"


def safe_filename(filename: str, used: set[str] | None = None) -> str:
    """Filesystem-safe unique filename under inputs/attachments/."""
    used = used if used is not None else set()
    base = Path(filename or "attachment.bin").name
    base = re.sub(r"[^\w.\-()+ ]+", "_", base).strip() or "attachment.bin"
    candidate = base
    n = 1
    while candidate.lower() in {u.lower() for u in used}:
        stem = Path(base).stem
        suffix = Path(base).suffix
        candidate = f"{stem}_{n}{suffix}"
        n += 1
    used.add(candidate)
    return candidate


def write_attachments_manifest(run_dir: Path, attachments: list[dict[str, Any]]) -> Path:
    """Persist inputs/attachments.json — canonical SoT index for the run."""
    inputs = run_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    items = []
    for att in attachments:
        rel = att.get("storedPath") or (
            f"inputs/attachments/{att['storedAs']}" if att.get("storedAs") else None
        )
        items.append(
            {
                "filename": att.get("filename"),
                "role": att.get("role") or infer_attachment_role(att.get("filename") or "", att.get("mimeType")),
                "mimeType": att.get("mimeType"),
                "storedAs": att.get("storedAs"),
                "path": rel,
                "sourceOfTruth": bool(att.get("storedAs")),
                "error": att.get("error"),
            }
        )
    primary_html = next((i["path"] for i in items if i.get("role") == "html" and i.get("path")), None)
    primary_image = next(
        (
            i["path"]
            for i in items
            if i.get("role") in ("image", "wireframe", "mockup", "before", "after") and i.get("path")
        ),
        None,
    )
    design_notes = [i["path"] for i in items if i.get("role") == "design_notes" and i.get("path")]
    payload = {
        "source": "jira_attachments",
        "attachments": items,
        "primaryHtml": primary_html,
        "primaryImage": primary_image,
        "designNotes": design_notes,
        "ready": bool(items) and all(i.get("sourceOfTruth") for i in items if not i.get("error")),
        "missing": [i.get("filename") for i in items if not i.get("sourceOfTruth")],
    }
    # Prefer canonical page.html when present (promoted from first HTML attachment).
    page = inputs / "page.html"
    if page.exists():
        payload["primaryHtml"] = "inputs/page.html"
    out = inputs / "attachments.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def collect_source_of_truth(run_dir: Path) -> dict[str, Any]:
    """Load SoT catalog for requirements / plan / mediation."""
    manifest_path = run_dir / "inputs" / "attachments.json"
    catalog: dict[str, Any] = {
        "attachments": [],
        "primaryHtml": None,
        "primaryImage": None,
        "designNotes": [],
        "images": [],
        "ready": True,
        "missing": [],
    }
    if manifest_path.exists():
        try:
            catalog.update(json.loads(manifest_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    page = run_dir / "inputs" / "page.html"
    if page.exists():
        catalog["primaryHtml"] = "inputs/page.html"

    images_manifest = run_dir / "inputs" / "images.json"
    if images_manifest.exists():
        try:
            data = json.loads(images_manifest.read_text(encoding="utf-8"))
            catalog["images"] = data.get("images") or []
            if not catalog.get("primaryImage") and catalog["images"]:
                catalog["primaryImage"] = catalog["images"][0].get("path")
        except (json.JSONDecodeError, OSError):
            pass

    # If Jira listed attachments but files are not on disk, mark not ready.
    jira_path = run_dir / "inputs" / "jira.raw.json"
    if jira_path.exists():
        try:
            jira = json.loads(jira_path.read_text(encoding="utf-8"))
            listed = jira.get("attachments") or []
            still_missing: list[str] = []
            for a in listed:
                stored = a.get("storedAs")
                filename = a.get("filename") or stored or "?"
                if stored:
                    p1 = run_dir / "inputs" / "attachments" / stored
                    p2 = run_dir / "inputs" / stored
                    if p1.exists() or p2.exists():
                        continue
                    still_missing.append(filename)
                    continue
                # Manual remediation: HTML provided via add_html → inputs/page.html.
                role = a.get("role") or infer_attachment_role(filename, a.get("mimeType"))
                if role == "html" and (run_dir / "inputs" / "page.html").exists():
                    continue
                still_missing.append(filename)
            catalog["missing"] = still_missing
            catalog["ready"] = len(still_missing) == 0
        except (json.JSONDecodeError, OSError):
            pass

    return catalog


def missing_sot_attachments(run_dir: Path) -> list[str]:
    """Filenames of Jira attachments that must exist on disk but do not."""
    return list(collect_source_of_truth(run_dir).get("missing") or [])


def sot_block_message(missing: list[str]) -> str:
    names = ", ".join(f"'{m}'" for m in missing)
    return (
        f"BLOCKED: source-of-truth Jira attachment(s) not stored locally: {names}. "
        "UiForgeMax must download attachments into the run temp dir "
        "(`inputs/attachments/`) before classify/plan/implement. "
        "Fix Jira credentials / network and re-call uiforgemax_add_jira, "
        "or supply files via uiforgemax_add_html / uiforgemax_add_image."
    )
