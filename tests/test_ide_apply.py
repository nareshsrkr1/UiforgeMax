"""IDE apply mode — verify planned paths after agent edits."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.pipeline.ide_apply import (
    build_pre_apply_baseline,
    ide_apply_brief,
    partition_mcp_writable,
    verify_ide_apply,
)
from uiforgemax.pipeline.planning import plan_all_mcp_writable, plan_is_implementable


def test_intent_only_plan_is_approvable():
    plan = {
        "create": [{"path": "a.ts", "purpose": "new"}],
        "modify": [{"path": "b.ts", "purpose": "tweak", "changeSummary": "rename"}],
    }
    assert plan_is_implementable(plan) is True


def test_content_bodies_are_not_mcp_writable():
    """Embedded content must not route to MCP apply_plan (loop source)."""
    plan = {
        "create": [{"path": "a.ts", "purpose": "x", "content": "export const a=1\n"}],
        "modify": [{"path": "b.ts", "purpose": "y", "content": "export const b=2\n"}],
    }
    assert plan_all_mcp_writable(plan) is False
    mcp, ide = partition_mcp_writable(plan)
    assert mcp["create"] == [] and mcp["modify"] == []
    assert len(ide["create"]) == 1 and len(ide["modify"]) == 1


def test_failed_empty_plan_is_not_resurrected_to_ide_apply(tmp_path: Path):
    """Empty-plan FAILED must stay terminal — no empty ideApplyBrief recovery."""
    import os
    import tempfile

    from uiforgemax.config import Config
    from uiforgemax.state import Stage, Status
    from uiforgemax.tools import ToolContext, lifecycle, pipeline, preflight

    runs = tempfile.mkdtemp()
    ctx = ToolContext.from_config(Config(runs_root=runs))
    os.environ["UIFORGEMAX_SKIP_MEDIATION"] = "1"
    proj = tmp_path / "p"
    proj.mkdir()
    preflight.preflight(ctx, project_root=str(proj))
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(proj)))["runId"]
    run_dir = ctx.store.run_dir(run_id)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": []}), encoding="utf-8"
    )
    state = ctx.store.load(run_id)
    state.status = Status.FAILED
    state.current_stage = Stage.IMPLEMENT
    ctx.store.save(state)
    out = json.loads(pipeline.advance(ctx, run_id))
    assert out["status"] == Status.FAILED.value
    assert out.get("waitForIdeApply") is not True


def test_post_approval_targets_approved_plan_only(tmp_path: Path):
    """After freeze, draft mutations must not change IDE-apply / implement targets."""
    from uiforgemax.pipeline.planning import load_locked_plan

    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    approved = {
        "create": [{"path": "approved.ts", "purpose": "keep"}],
        "modify": [],
    }
    draft = {
        "create": [{"path": "drifted.ts", "purpose": "should not win"}],
        "modify": [{"path": "extra.ts", "purpose": "no"}],
    }
    (run_dir / "plans" / "approved-plan.json").write_text(json.dumps(approved), encoding="utf-8")
    (run_dir / "plans" / "implementation-plan.json").write_text(json.dumps(draft), encoding="utf-8")

    locked = load_locked_plan(run_dir, require_approved=True)
    assert [a["path"] for a in locked.get("create") or []] == ["approved.ts"]
    assert locked.get("modify") == []

    brief = ide_apply_brief(locked)
    assert brief["filesToCreate"][0]["path"] == "approved.ts"
    assert all(f["path"] != "drifted.ts" for f in brief["filesToCreate"])


def test_ide_apply_no_progress_circuit_breaker(tmp_path: Path):
    """N blind advances with unchanged incompletePaths → BLOCKED."""
    import os
    import tempfile

    from uiforgemax.config import Config
    from uiforgemax.state import Stage, Status
    from uiforgemax.tools import ToolContext, lifecycle, pipeline, preflight
    from uiforgemax.tools.pipeline import _IDE_APPLY_NO_PROGRESS_LIMIT

    runs = tempfile.mkdtemp()
    ctx = ToolContext.from_config(Config(runs_root=runs))
    os.environ["UIFORGEMAX_SKIP_MEDIATION"] = "1"
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "b.ts").write_text("old\n", encoding="utf-8")
    preflight.preflight(ctx, project_root=str(proj))
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(proj)))["runId"]
    run_dir = ctx.store.run_dir(run_id)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    plan = {
        "create": [],
        "modify": [{"path": "b.ts", "purpose": "update", "changeSummary": "change text"}],
    }
    (run_dir / "plans" / "approved-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    from uiforgemax.pipeline.ide_apply import write_pre_apply_baseline

    write_pre_apply_baseline(run_dir, {"default": proj}, plan)
    state = ctx.store.load(run_id)
    state.status = Status.AWAITING_IDE_APPLY
    state.current_stage = Stage.IMPLEMENT
    state.artifacts["ideApply"] = True
    state.approvals.plan.approved = True
    ctx.store.save(state)

    last = None
    for _ in range(_IDE_APPLY_NO_PROGRESS_LIMIT):
        last = json.loads(pipeline.advance(ctx, run_id))
    assert last is not None
    assert last["status"] == Status.BLOCKED.value
    assert last.get("circuitBreaker") == "ide_apply_no_progress"


def test_verify_ide_apply_detects_create_and_modify(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "b.ts").write_text("old\n", encoding="utf-8")
    plan = {
        "create": [{"path": "a.ts", "purpose": "new file"}],
        "modify": [{"path": "b.ts", "purpose": "update"}],
    }
    roots = {"default": root}
    baseline = build_pre_apply_baseline(roots, plan)
    (root / "a.ts").write_text("created\n", encoding="utf-8")
    (root / "b.ts").write_text("new\n", encoding="utf-8")
    summary, problems = verify_ide_apply(roots, plan, baseline)
    assert not problems
    assert summary["fileCount"] == 2
    assert set(summary["filesChanged"]) == {"a.ts", "b.ts"}


def test_verify_ide_apply_flags_unchanged(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "b.ts").write_text("same\n", encoding="utf-8")
    plan = {"create": [], "modify": [{"path": "b.ts", "purpose": "update"}]}
    roots = {"default": root}
    baseline = build_pre_apply_baseline(roots, plan)
    summary, problems = verify_ide_apply(roots, plan, baseline)
    assert problems == ["b.ts"]
    assert summary["fileCount"] == 0


def test_ide_apply_brief_lists_paths():
    brief = ide_apply_brief(
        {
            "create": [{"path": "a.ts", "purpose": "x"}],
            "modify": [{"path": "b.ts", "purpose": "y", "changeSummary": "z"}],
            "executionOrder": ["b.ts", "a.ts"],
        }
    )
    assert brief["mode"] == "ide_apply"
    assert brief["filesToCreate"][0]["path"] == "a.ts"
    assert brief["filesToModify"][0]["changeSummary"] == "z"
