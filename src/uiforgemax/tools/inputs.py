"""Input tools (Stage 0 intake)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from uiforgemax.adapters.jira import download_attachments, fetch_issue
from uiforgemax.intake_hints import extract_leading_issue_key
from uiforgemax.mcp_response import tool_response
from uiforgemax.pipeline.source_of_truth import (
    infer_attachment_role,
    sot_block_message,
    write_attachments_manifest,
)
from uiforgemax.state.gates import assert_gate
from uiforgemax.tools.context import ToolContext


def add_prompt(ctx: ToolContext, run_id: str, text: str) -> str:
    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_add_prompt", state)

    # When Jira is configured, do not let agents invent a ticket body via add_prompt.
    key = extract_leading_issue_key(text)
    modes = state.inputs.get("modes") or []
    already_jira = "jira" in modes
    if key and ctx.config.jira.is_configured and not already_jira:
        state.inputs["pendingIssueKey"] = key
        ctx.store.save(state)
        return tool_response(
            state,
            (
                f"BLOCKED: text starts with Jira key '{key}'. "
                f"Call uiforgemax_add_jira(run_id='{run_id}', issue_key='{key}') "
                "to fetch the real issue — do not invent requirements via add_prompt. "
                "After Jira is attached you may add_prompt for extra human notes."
            ),
            stop=True,
            jira_configured=True,
            extra={
                "suggestedIssueKey": key,
                "redirectTool": "uiforgemax_add_jira",
            },
        )

    path = ctx.store.run_dir(run_id) / "inputs" / "prompt.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    state.add_mode("prompt")
    state.record(state.current_stage, "input_added", "prompt")
    ctx.store.save(state)
    return tool_response(
        state,
        f"Prompt saved. Modes: {state.inputs['modes']}",
        jira_configured=ctx.config.jira.is_configured,
    )


def add_html(ctx: ToolContext, run_id: str, html: str) -> str:
    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_add_html", state)
    run_dir = ctx.store.run_dir(run_id)
    path = run_dir / "inputs" / "page.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    # Also register under attachments SoT catalog when present.
    att_dir = run_dir / "inputs" / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    dest = att_dir / "page.html"
    dest.write_text(html, encoding="utf-8")
    # Patch Jira attachment rows that were HTML but not downloaded.
    jira_path = run_dir / "inputs" / "jira.raw.json"
    if jira_path.exists():
        try:
            jira = json.loads(jira_path.read_text(encoding="utf-8"))
            for att in jira.get("attachments") or []:
                role = att.get("role") or infer_attachment_role(
                    att.get("filename") or "", att.get("mimeType")
                )
                if role == "html" and not att.get("storedAs"):
                    name = Path(att.get("filename") or "page.html").name
                    target = att_dir / name
                    if not target.exists():
                        shutil.copy2(dest, target)
                    att["storedAs"] = name
                    att["storedPath"] = f"inputs/attachments/{name}"
                    att["role"] = "html"
                    att["sourceOfTruth"] = True
                    att.pop("error", None)
            jira_path.write_text(json.dumps(jira, indent=2), encoding="utf-8")
            write_attachments_manifest(run_dir, list(jira.get("attachments") or []))
            missing = [a.get("filename") for a in (jira.get("attachments") or []) if not a.get("storedAs")]
            state.inputs.setdefault("jira", {})["attachmentsMissing"] = missing
            state.inputs["jira"]["attachmentsReady"] = not bool(missing)
        except (json.JSONDecodeError, OSError):
            write_attachments_manifest(
                run_dir,
                [
                    {
                        "filename": "page.html",
                        "mimeType": "text/html",
                        "role": "html",
                        "storedAs": "page.html",
                        "storedPath": "inputs/attachments/page.html",
                        "sourceOfTruth": True,
                    }
                ],
            )
    else:
        write_attachments_manifest(
            run_dir,
            [
                {
                    "filename": "page.html",
                    "mimeType": "text/html",
                    "role": "html",
                    "storedAs": "page.html",
                    "storedPath": "inputs/attachments/page.html",
                    "sourceOfTruth": True,
                }
            ],
        )
    state.add_mode("html")
    state.record(state.current_stage, "input_added", "html")
    ctx.store.save(state)
    return tool_response(state, f"HTML saved as SoT (inputs/page.html). Modes: {state.inputs['modes']}")


_IMAGE_ROLES = {"reference", "before", "after", "wireframe", "mockup"}


def _record_image_role(run_dir: Path, rel_path: str, role: str) -> None:
    """Track image roles in inputs/images.json so before/after pairs carry through."""
    manifest = run_dir / "inputs" / "images.json"
    data: dict = {"images": []}
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {"images": []}
    images = [img for img in data.get("images", []) if img.get("path") != rel_path]
    images.append({"path": rel_path, "role": role})
    data["images"] = images
    manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")


def add_image(ctx: ToolContext, run_id: str, source_path: str, role: str = "reference") -> str:
    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_add_image", state)
    role = (role or "reference").strip().lower()
    if role not in _IMAGE_ROLES:
        return tool_response(
            state,
            f"BLOCKED: role must be one of {sorted(_IMAGE_ROLES)}, got '{role}'.",
            stop=True,
        )
    src = Path(source_path)
    if not src.exists():
        return tool_response(state, f"BLOCKED: image not found at {source_path}", stop=True)
    run_dir = ctx.store.run_dir(run_id)
    att_dir = run_dir / "inputs" / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    dest = att_dir / src.name
    shutil.copy2(src, dest)
    # Keep a copy under inputs/ for mediation image discovery.
    inputs_copy = run_dir / "inputs" / src.name
    if inputs_copy.resolve() != dest.resolve():
        shutil.copy2(src, inputs_copy)
    rel = f"inputs/attachments/{src.name}"
    _record_image_role(run_dir, rel, role)
    _record_image_role(run_dir, f"inputs/{src.name}", role)
    state.add_mode("image")
    state.record(state.current_stage, "input_added", f"image:{src.name} ({role})")
    ctx.store.save(state)
    return tool_response(
        state,
        f"Image copied to {dest} as role='{role}' (SoT). Interpreted during classify/normalize.",
    )


def _promote_html_and_images(run_dir: Path, attachments: list[dict]) -> None:
    """Copy first HTML → inputs/page.html; register image attachments for mediation."""
    inputs_dir = run_dir / "inputs"
    for att in attachments:
        if not att.get("storedAs"):
            continue
        src = inputs_dir / "attachments" / att["storedAs"]
        if not src.exists():
            continue
        role = att.get("role") or infer_attachment_role(att.get("filename") or "", att.get("mimeType"))
        if role == "html":
            page = inputs_dir / "page.html"
            if not page.exists():
                shutil.copy2(src, page)
        if role in ("image", "wireframe", "mockup", "before", "after"):
            # Mediation scans inputs/* for images — mirror there too.
            mirror = inputs_dir / att["storedAs"]
            if not mirror.exists():
                shutil.copy2(src, mirror)
            rel_att = f"inputs/attachments/{att['storedAs']}"
            img_role = role if role in _IMAGE_ROLES else "reference"
            if role == "image":
                img_role = "reference"
            _record_image_role(run_dir, rel_att, img_role)
            _record_image_role(run_dir, f"inputs/{att['storedAs']}", img_role)


def add_jira(ctx: ToolContext, run_id: str, issue_key: str) -> str:
    state = ctx.store.load(run_id)
    assert_gate("uiforgemax_add_jira", state)
    try:
        jira_raw = fetch_issue(issue_key, ctx.config.jira)
    except RuntimeError as exc:
        return tool_response(
            state,
            f"BLOCKED (config): {exc}",
            stop=True,
            jira_configured=ctx.config.jira.is_configured,
        )

    run_dir = ctx.store.run_dir(run_id)
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)

    listed = list(jira_raw.get("attachments") or [])
    att_dir = inputs_dir / "attachments"
    fixture_root = Path(os.environ["UIFORGEMAX_FIXTURES_ROOT"]) if os.getenv("UIFORGEMAX_FIXTURES_ROOT") else None
    stored, errors = download_attachments(
        listed,
        att_dir,
        ctx.config.jira,
        fixture_root=fixture_root,
    )
    jira_raw["attachments"] = stored
    (inputs_dir / "jira.raw.json").write_text(json.dumps(jira_raw, indent=2), encoding="utf-8")
    _promote_html_and_images(run_dir, stored)
    # Manifest after promote so primaryHtml prefers inputs/page.html when present.
    write_attachments_manifest(run_dir, stored)

    copied = [a["storedAs"] for a in stored if a.get("storedAs")]
    missing = [a.get("filename") or "?" for a in stored if not a.get("storedAs")]

    state.add_mode("jira")
    state.inputs.setdefault("jira", {})["issueKey"] = issue_key
    state.inputs["jira"]["attachmentsStored"] = copied
    state.inputs["jira"]["attachmentsMissing"] = missing
    state.inputs["jira"]["attachmentsReady"] = not bool(missing)
    state.inputs.pop("pendingIssueKey", None)

    # Hard-block when the ticket declares attachments that are SoT but not on disk.
    if listed and missing:
        state.record(state.current_stage, "blocked", f"jira attachments missing: {missing}")
        ctx.store.save(state)
        return tool_response(
            state,
            sot_block_message(missing) + (f" Errors: {errors}" if errors else ""),
            stop=True,
            jira_configured=ctx.config.jira.is_configured,
            extra={
                "attachmentsStored": copied,
                "attachmentsMissing": missing,
                "attachmentErrors": errors,
                "attachmentsDir": str(att_dir),
                "sourceOfTruth": "inputs/attachments.json",
                "nextTool": "uiforgemax_add_html",
                "alternatives": ["uiforgemax_add_image", "uiforgemax_add_jira"],
            },
        )

    state.record(
        state.current_stage,
        "input_added",
        f"jira:{issue_key} attachments={copied or 'none'}",
    )
    ctx.store.save(state)
    return tool_response(
        state,
        (
            f"Jira {issue_key} fetched → jira.raw.json. "
            f"Attachments stored under inputs/attachments/: {copied or 'none'}."
        ),
        jira_configured=ctx.config.jira.is_configured,
        extra={
            "attachmentsStored": copied,
            "attachmentsDir": str(att_dir),
            "sourceOfTruth": "inputs/attachments.json",
        },
    )
