"""Multi-repo workspace support: a plan can reference more than one repo root.

Covers the generic guardrail the user asked for: if a plan action names a
root that isn't part of the run yet, the PLAN stage must BLOCK and ask for it
via `uiforgemax_add_workspace_root` — never silently assume, guess, or fail.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.implement import _normalize_roots, apply_plan, resolve_root
from uiforgemax.state import Stage, Status
from uiforgemax.stages.runner import _missing_workspace_roots
from uiforgemax.tools import ToolContext, approvals, inputs, lifecycle, pipeline, preflight


def _ctx() -> ToolContext:
    runs = tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    os.environ["UIFORGEMAX_SKIP_MEDIATION"] = "1"
    return ToolContext.from_config(cfg)


def _parse_run_id(response: str) -> str:
    return json.loads(response)["runId"]


def test_resolve_root_defaults_when_unset():
    roots = {"default": Path("/repo-a")}
    assert resolve_root(roots, {"path": "x.py"}) == Path("/repo-a")


def test_resolve_root_uses_named_root():
    roots = {"default": Path("/repo-a"), "other": Path("/repo-b")}
    assert resolve_root(roots, {"path": "x.py", "root": "other"}) == Path("/repo-b")


def test_normalize_roots_wraps_plain_path():
    assert _normalize_roots(Path("/repo-a")) == {"default": Path("/repo-a")}


def test_missing_workspace_roots_flags_unregistered_names():
    class _S:
        project_roots: dict = {}

    actions = [{"path": "a.py"}, {"path": "b.py", "root": "other-repo"}]
    assert _missing_workspace_roots(actions, _S()) == ["other-repo"]


def test_missing_workspace_roots_ignores_registered_names():
    class _S:
        project_roots = {"other-repo": "/repo-b"}

    actions = [{"path": "b.py", "root": "other-repo"}]
    assert _missing_workspace_roots(actions, _S()) == []


def test_apply_plan_writes_into_the_correct_named_root(tmp_path):
    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()
    plan = {
        "create": [{"path": "README.md", "templateId": "greenfield.readme", "root": "extra"}],
        "modify": [],
        "executionOrder": ["README.md"],
    }
    summary = apply_plan({"default": root_a, "extra": root_b}, plan)
    assert (root_b / "README.md").exists()
    assert not (root_a / "README.md").exists()
    assert summary["fileCount"] == 1


def test_plan_stage_blocks_then_resumes_after_add_workspace_root(tmp_path):
    """End-to-end: PLAN blocks on an unregistered root, add_workspace_root
    resolves it, and IMPLEMENT writes the file into the *correct* repo."""
    ctx = _ctx()
    primary = tmp_path / "primary-app"
    primary.mkdir()
    extra_repo = tmp_path / "extra-repo"
    extra_repo.mkdir()

    run_id = _parse_run_id(lifecycle.start_run(ctx, project_root=str(primary)))
    inputs.add_prompt(ctx, run_id, "Build a small greenfield app from scratch")

    # Drive to sole plan gate (understanding auto-passes).
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.AWAITING_PLAN_APPROVAL

    # Inject a multi-root plan and re-enter PLAN so missing-root BLOCK fires.
    from uiforgemax.state import Stage

    run_dir = ctx.store.run_dir(run_id)
    plan = {
        "source": "test_injected",
        "create": [
            {
                "path": "EXTRA-README.md",
                "purpose": "lives in the other repo",
                "templateId": "greenfield.readme",
                "root": "extra-repo",
            }
        ],
        "modify": [],
        "executionOrder": ["EXTRA-README.md"],
        "tests": {"unit": [], "compliance": {}},
    }
    (run_dir / "plans" / "implementation-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (run_dir / "plans" / "plan-review.json").write_text(
        json.dumps({"verdict": "pass", "coverage": {}, "risks": [], "requiredPlanChanges": []}),
        encoding="utf-8",
    )
    state.approvals.plan.approved = False
    state.current_stage = Stage.PLAN
    state.status = Status.PLAN_READY
    ctx.store.save(state)

    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.BLOCKED
    assert "extra-repo" in (state.history[-1].detail or "")

    preflight.add_workspace_root(ctx, run_id, "extra-repo", str(extra_repo))
    state = ctx.store.load(run_id)
    assert state.project_roots["extra-repo"] == str(extra_repo.resolve())

    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.AWAITING_PLAN_APPROVAL

    approvals.approve_plan(ctx, run_id)
    pipeline.advance(ctx, run_id)
    state = ctx.store.load(run_id)
    assert state.status == Status.COMPLETED
    assert (extra_repo / "EXTRA-README.md").exists()
    assert not (primary / "EXTRA-README.md").exists()


def test_start_run_with_components_seeds_project_roots_and_indexes_each(tmp_path):
    """Upfront components get real graphify-out/ + run-dir by-root copies."""
    from uiforgemax.graphify.cli_runner import ensure_graphify_available
    from uiforgemax.session import load_session, save_session
    import pytest

    if not ensure_graphify_available().get("ok"):
        pytest.skip("graphifyy not installed")

    ctx = _ctx()
    parent = tmp_path / "Project"
    ui = parent / "ui"
    backend = parent / "backend"
    ui.mkdir(parents=True)
    backend.mkdir(parents=True)
    (ui / "main.py").write_text("def ui():\n    return 1\n", encoding="utf-8")
    (backend / "main.py").write_text("def api():\n    return 1\n", encoding="utf-8")

    original = load_session()
    try:
        resp = json.loads(
            lifecycle.start_run(
                ctx,
                project_root=str(parent),
                components=json.dumps({"ui": str(ui), "backend": str(backend)}),
            )
        )
        run_id = resp["runId"]
        state = ctx.store.load(run_id)
        assert state.project_roots["ui"] == str(ui.resolve())
        assert state.project_roots["backend"] == str(backend.resolve())

        inputs.add_prompt(ctx, run_id, "Enhance both the ui and backend")
        pipeline.advance(ctx, run_id)

        run_dir = ctx.store.run_dir(run_id)
        assert (ui / "graphify-out" / "graph.json").exists()
        assert (backend / "graphify-out" / "graph.json").exists()
        assert (run_dir / "graph" / "by-root" / "ui" / "graph.json").exists()
        assert (run_dir / "graph" / "by-root" / "backend" / "graph.json").exists()
    finally:
        save_session(original)


def test_start_run_blocks_component_outside_ide_workspace_folders(tmp_path):
    from uiforgemax.session import load_session, save_session

    ctx = _ctx()
    parent = tmp_path / "Project"
    outside = tmp_path / "elsewhere"
    parent.mkdir()
    outside.mkdir()

    original = load_session()
    try:
        preflight.preflight(
            ctx,
            project_root=str(parent),
            workspace_folders=json.dumps([str(parent)]),
        )
        resp = json.loads(
            lifecycle.start_run(
                ctx,
                project_root=str(parent),
                components=json.dumps({"other": str(outside)}),
            )
        )
        assert resp.get("ok") is False or "BLOCKED" in resp.get("message", "")
        assert "other" in resp.get("message", "")
    finally:
        # Global session.json is shared across tests in this process — restore it
        # so this test's `ideWorkspaceFolders` restriction doesn't leak elsewhere.
        save_session(original)
