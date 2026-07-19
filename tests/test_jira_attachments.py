"""Jira attachment download → run SoT inputs + hard-block when missing."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import httpx
import pytest

from uiforgemax.adapters.jira import download_attachments
from uiforgemax.config import Config, JiraConfig
from uiforgemax.pipeline.source_of_truth import (
    collect_source_of_truth,
    infer_attachment_role,
    missing_sot_attachments,
)
from uiforgemax.tools import ToolContext, inputs, lifecycle


def test_infer_attachment_roles():
    assert infer_attachment_role("test.html", "text/html") == "html"
    assert infer_attachment_role("wireframe-v1.png", "image/png") == "wireframe"
    assert infer_attachment_role("screen_before.png") == "before"
    assert infer_attachment_role("CIB_Data_Marketplace_Production_Plan.md") == "design_notes"


def test_download_attachments_from_local_path():
    root = Path(tempfile.mkdtemp())
    src = root / "fixtures" / "test.html"
    src.parent.mkdir(parents=True)
    src.write_text("<html><body>SoT</body></html>", encoding="utf-8")
    dest = root / "attachments"
    atts = [{"filename": "test.html", "path": str(src), "mimeType": "text/html"}]
    updated, errors = download_attachments(atts, dest, JiraConfig())
    assert not errors
    assert updated[0]["storedAs"] == "test.html"
    assert updated[0]["role"] == "html"
    assert updated[0]["sourceOfTruth"] is True
    assert (dest / "test.html").read_text(encoding="utf-8").startswith("<html>")


def test_download_attachments_from_url(monkeypatch):
    class _Resp:
        content = b"<html>remote</html>"

        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, auth=None):
            assert "attachment/content" in url
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    dest = Path(tempfile.mkdtemp()) / "attachments"
    cfg = JiraConfig(base_url="https://example.atlassian.net", email="a@b.c", api_token="tok")
    atts = [
        {
            "filename": "test.html",
            "url": "https://example.atlassian.net/rest/api/3/attachment/content/1",
            "mimeType": "text/html",
        }
    ]
    updated, errors = download_attachments(atts, dest, cfg)
    assert errors == []
    assert updated[0]["storedAs"] == "test.html"
    assert (dest / "test.html").read_bytes() == b"<html>remote</html>"


def test_download_missing_url_reports_error():
    dest = Path(tempfile.mkdtemp()) / "attachments"
    atts = [{"filename": "gone.html", "mimeType": "text/html"}]
    updated, errors = download_attachments(atts, dest, JiraConfig())
    assert errors
    assert updated[0]["sourceOfTruth"] is False
    assert not (dest / "gone.html").exists()


def test_add_jira_stores_attachments_and_blocks_when_missing(monkeypatch):
    runs = tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    os.environ["UIFORGEMAX_USE_FIXTURES"] = "1"
    fixtures = Path(tempfile.mkdtemp()) / "fixtures"
    (fixtures / "jira").mkdir(parents=True)
    html = fixtures / "jira" / "ref.html"
    html.write_text("<html>ok</html>", encoding="utf-8")
    issue = {
        "key": "SCRUM-ATTACH",
        "summary": "With HTML SoT",
        "labels": [],
        "description": "Use attached test.html",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Match HTML"}],
        "comments": [],
        "attachments": [
            {
                "filename": "test.html",
                "path": str(html),
                "mimeType": "text/html",
            }
        ],
        "customFields": {"resolutionPolicy": "frontend_first", "matchExactly": False},
    }
    (fixtures / "jira" / "SCRUM-ATTACH.json").write_text(json.dumps(issue), encoding="utf-8")
    os.environ["UIFORGEMAX_FIXTURES_ROOT"] = str(fixtures)
    ctx = ToolContext.from_config(cfg)
    workspace = Path(tempfile.mkdtemp())
    resp = json.loads(lifecycle.start_run(ctx, project_root=str(workspace)))
    run_id = resp["runId"]
    out = json.loads(inputs.add_jira(ctx, run_id, "SCRUM-ATTACH"))
    assert out.get("stop") is False
    run_dir = ctx.store.run_dir(run_id)
    assert (run_dir / "inputs" / "attachments" / "test.html").exists()
    assert (run_dir / "inputs" / "page.html").exists()
    manifest = json.loads((run_dir / "inputs" / "attachments.json").read_text(encoding="utf-8"))
    assert manifest["primaryHtml"] == "inputs/page.html"
    assert missing_sot_attachments(run_dir) == []

    # Missing remote attachment → hard block
    issue2 = dict(issue)
    issue2["key"] = "SCRUM-MISS"
    issue2["attachments"] = [
        {
            "filename": "missing.html",
            "url": "https://example.atlassian.net/rest/api/3/attachment/content/999",
            "mimeType": "text/html",
        }
    ]
    (fixtures / "jira" / "SCRUM-MISS.json").write_text(json.dumps(issue2), encoding="utf-8")

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.HTTPError("denied")

    monkeypatch.setattr(httpx, "Client", _Boom)
    resp2 = json.loads(lifecycle.start_run(ctx, project_root=str(workspace)))
    blocked = json.loads(inputs.add_jira(ctx, resp2["runId"], "SCRUM-MISS"))
    assert blocked.get("stop") is True
    assert "BLOCKED" in blocked.get("message", "")
    assert "missing.html" in blocked.get("message", "")


def test_collect_source_of_truth_and_intake_block(monkeypatch):
    from uiforgemax.stages.runner import _intake
    from uiforgemax.state import RunState, Status

    runs = tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    ctx = ToolContext.from_config(cfg)
    workspace = Path(tempfile.mkdtemp())
    os.environ["UIFORGEMAX_USE_FIXTURES"] = "1"
    fixtures = Path(tempfile.mkdtemp()) / "fixtures"
    (fixtures / "jira").mkdir(parents=True)
    os.environ["UIFORGEMAX_FIXTURES_ROOT"] = str(fixtures)
    (fixtures / "jira" / "X-1.json").write_text(
        json.dumps(
            {
                "key": "X-1",
                "summary": "x",
                "labels": [],
                "description": "d",
                "acceptanceCriteria": [],
                "comments": [],
                "attachments": [{"filename": "a.html", "mimeType": "text/html"}],
                "customFields": {},
            }
        ),
        encoding="utf-8",
    )
    resp = json.loads(lifecycle.start_run(ctx, project_root=str(workspace)))
    run_id = resp["runId"]
    # Force incomplete attachments into run dir
    run_dir = ctx.store.run_dir(run_id)
    inputs_dir = run_dir / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    jira = {
        "key": "X-1",
        "attachments": [{"filename": "a.html", "mimeType": "text/html", "role": "html"}],
    }
    (inputs_dir / "jira.raw.json").write_text(json.dumps(jira), encoding="utf-8")
    state = ctx.store.load(run_id)
    state.add_mode("jira")
    state.inputs.setdefault("jira", {})["issueKey"] = "X-1"
    ctx.store.save(state)
    state = ctx.store.load(run_id)
    result = _intake(ctx, state)
    assert result.stop is True
    assert "BLOCKED" in result.message
    assert collect_source_of_truth(run_dir)["ready"] is False
