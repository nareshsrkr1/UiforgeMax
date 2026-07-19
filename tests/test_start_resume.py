"""start = clean slate; resume = continue existing run."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.state import Status
from uiforgemax.tools import ToolContext, lifecycle


def _ctx(tmp_path: Path) -> ToolContext:
    runs = tmp_path / "runs"
    runs.mkdir()
    return ToolContext.from_config(Config(runs_root=str(runs)))


def test_start_archives_prior_incomplete_and_creates_new(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ws = tmp_path / "ui"
    ws.mkdir()
    (ws / "index.html").write_text("<html></html>", encoding="utf-8")

    first = json.loads(lifecycle.start_run(ctx, project_root=str(ws), mode="start"))
    first_id = first["runId"]
    assert ctx.store.exists(first_id)

    second = json.loads(lifecycle.start_run(ctx, project_root=str(ws), mode="start"))
    second_id = second["runId"]
    assert second_id != first_id
    assert first_id in second.get("archivedRuns", [])
    assert not ctx.store.exists(first_id)
    assert (Path(ctx.config.runs_root) / "_archive" / first_id / "run.json").exists()
    assert ctx.store.exists(second_id)


def test_resume_continues_latest_matching_run(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ws = tmp_path / "ui"
    ws.mkdir()
    (ws / "index.html").write_text("<html></html>", encoding="utf-8")

    started = json.loads(lifecycle.start_run(ctx, project_root=str(ws), mode="start"))
    run_id = started["runId"]
    state = ctx.store.load(run_id)
    state.status = Status.AWAITING_MEDIATION
    ctx.store.save(state)

    resumed = json.loads(lifecycle.start_run(ctx, project_root=str(ws), mode="resume"))
    assert resumed["runId"] == run_id
    assert resumed.get("mode") == "resume" or "Resumed" in resumed.get("message", "")


def test_start_does_not_merge_stale_session_components(tmp_path: Path, monkeypatch):
    from uiforgemax import session as session_mod

    ctx = _ctx(tmp_path)
    ws = tmp_path / "ui"
    ws.mkdir()
    (ws / "index.html").write_text("x", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()

    sess_path = tmp_path / "session.json"
    sess_path.write_text(
        json.dumps(
            {
                "workspaceRoot": str(ws),
                "projectRoots": {"backend": str(other)},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_mod, "session_path", lambda: sess_path)

    resp = json.loads(lifecycle.start_run(ctx, project_root=str(ws), mode="start"))
    state = ctx.store.load(resp["runId"])
    assert state.project_roots == {}
