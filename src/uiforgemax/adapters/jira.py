"""Jira REST adapter (read-only). Falls back to local fixtures when configured."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import httpx

from uiforgemax.config import JiraConfig
from uiforgemax.pipeline.source_of_truth import infer_attachment_role, safe_filename

ACCEPTANCE_HEADING = re.compile(r"acceptance criteria", re.I)


def _request_auth(config: JiraConfig) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Return (headers, basic_auth) for Jira Cloud Basic or Server/DC Bearer."""
    headers = {"Accept": "application/json"}
    auth: tuple[str, str] | None = None
    if config.email:
        auth = (config.email, config.api_token or "")
    elif config.api_token:
        headers["Authorization"] = f"Bearer {config.api_token}"
    return headers, auth


def download_attachments(
    attachments: list[dict[str, Any]],
    dest_dir: Path,
    config: JiraConfig,
    *,
    fixture_root: Path | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Download/copy Jira attachments into ``dest_dir`` (run temp SoT folder).

    Returns ``(updated_attachments, errors)``. Each successful item gains
    ``storedAs``, ``storedPath`` (run-relative), ``role``, and ``sourceOfTruth``.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    errors: list[str] = []
    updated: list[dict[str, Any]] = []
    headers, auth = _request_auth(config)

    for att in attachments:
        item = dict(att)
        filename = item.get("filename") or "attachment.bin"
        role = infer_attachment_role(filename, item.get("mimeType"))
        item["role"] = role
        stored_name = safe_filename(filename, used)
        dest = dest_dir / stored_name
        ok = False

        # 1) Local path (fixtures / pre-staged)
        rel = item.get("path")
        if rel:
            src = Path(rel)
            if not src.is_absolute():
                roots = [fixture_root] if fixture_root else []
                roots.append(Path(__file__).resolve().parents[3])
                for root in roots:
                    if root and (root / rel).exists():
                        src = root / rel
                        break
            if src.exists() and src.is_file():
                shutil.copy2(src, dest)
                ok = True

        # 2) Remote content URL (Atlassian Cloud / Server)
        url = item.get("url") or item.get("content")
        if not ok and url and str(url).startswith(("http://", "https://")):
            try:
                with httpx.Client(timeout=60.0, follow_redirects=True) as client:
                    resp = client.get(url, headers=headers, auth=auth)
                    resp.raise_for_status()
                    dest.write_bytes(resp.content)
                    ok = True
            except httpx.HTTPError as exc:
                item["error"] = f"download failed: {exc}"
                errors.append(f"{filename}: {item['error']}")

        if ok:
            item["storedAs"] = stored_name
            item["storedPath"] = f"inputs/attachments/{stored_name}"
            item["sourceOfTruth"] = True
            item.pop("error", None)
        else:
            item["sourceOfTruth"] = False
            if "error" not in item:
                item["error"] = "no local path and no downloadable url"
                errors.append(f"{filename}: {item['error']}")
        updated.append(item)

    return updated, errors


def _fixture_path(issue_key: str) -> Path | None:
    env = os.getenv("UIFORGEMAX_FIXTURES_ROOT")
    roots = []
    if env:
        roots.append(Path(env))
    # repo-relative fixtures when developing UiForgeMax itself
    roots.append(Path(__file__).resolve().parents[3] / "fixtures")
    for root in roots:
        candidate = root / "jira" / f"{issue_key}.json"
        if candidate.exists():
            return candidate
    return None


def _adf_to_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_text(c) for c in node.get("content", [])]
        return "".join(parts)
    if isinstance(node, list):
        return "".join(_adf_to_text(c) for c in node)
    return ""


def _parse_acceptance_criteria(description: str) -> list[dict[str, str]]:
    lines = description.splitlines()
    ac: list[dict[str, str]] = []
    in_ac = False
    idx = 0
    for line in lines:
        if ACCEPTANCE_HEADING.search(line):
            in_ac = True
            continue
        if in_ac and line.strip():
            if line.strip().startswith("#"):
                break
            idx += 1
            ac.append({"id": f"AC-{idx}", "text": line.strip().lstrip("- ").strip()})
    if not ac and "Given" in description:
        for block in re.split(r"\n\s*\n", description):
            if block.strip().startswith("Given"):
                idx += 1
                ac.append({"id": f"AC-{idx}", "text": block.strip()})
    return ac


def _normalize_issue(raw: dict[str, Any]) -> dict[str, Any]:
    if "key" in raw and "fields" not in raw:
        return raw

    fields = raw.get("fields", {})
    description = _adf_to_text(fields.get("description"))
    comments = []
    comment_block = fields.get("comment", {}).get("comments", [])
    for c in comment_block:
        comments.append(
            {
                "author": c.get("author", {}).get("displayName", "unknown"),
                "created": c.get("created"),
                "body": _adf_to_text(c.get("body")),
            }
        )

    labels = fields.get("labels", [])
    ac = _parse_acceptance_criteria(description)
    if not ac:
        ac = [{"id": "AC-1", "text": fields.get("summary", "Implement ticket")}]

    attachments = []
    for att in fields.get("attachment", []) or []:
        attachments.append(
            {
                "filename": att.get("filename"),
                "url": att.get("content"),
                "mimeType": att.get("mimeType"),
            }
        )

    return {
        "key": raw.get("key"),
        "summary": fields.get("summary", ""),
        "labels": labels,
        "description": description,
        "acceptanceCriteria": ac,
        "comments": comments,
        "attachments": attachments,
        "customFields": {
            "matchExactly": "match-exactly" in labels or "match exactly" in description.lower(),
            "resolutionPolicy": os.getenv("UIFORGEMAX_DEFAULT_POLICY") or "pending",
            "targetApp": "customer-portal",
            "targetDomain": "customer",
        },
    }


def fetch_issue(issue_key: str, config: JiraConfig) -> dict[str, Any]:
    """Fetch a Jira issue. Uses REST when configured; otherwise local fixture."""
    use_fixtures = os.getenv("UIFORGEMAX_USE_FIXTURES", "").lower() in ("1", "true", "yes")

    if config.is_configured and not use_fixtures:
        # Cloud (email+token) supports /rest/api/3/; Server/Data Center (PAT-only,
        # no email) only ever supports /rest/api/2/ — /3/ 404s there.
        api_version = "2" if config.uses_bearer else "3"
        url = f"{config.base_url.rstrip('/')}/rest/api/{api_version}/issue/{issue_key}"
        params = {"fields": "summary,description,labels,attachment,comment"}
        headers, auth = _request_auth(config)
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url, params=params, auth=auth, headers=headers)
                resp.raise_for_status()
                return _normalize_issue(resp.json())
        except httpx.HTTPError:
            fixture = _fixture_path(issue_key)
            if fixture:
                return json.loads(fixture.read_text(encoding="utf-8"))

    fixture = _fixture_path(issue_key)
    if fixture:
        return json.loads(fixture.read_text(encoding="utf-8"))

    if not config.is_configured:
        raise RuntimeError(
            "Jira not configured and no local fixture found. "
            "Set JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN or UIFORGEMAX_USE_FIXTURES=1."
        )
    raise RuntimeError(f"Failed to fetch {issue_key} and no fixture available.")
