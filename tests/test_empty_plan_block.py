"""Empty create/modify plans must not reach approval or fake-complete implement."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.planning import (
    EMPTY_PLAN_BLOCKER,
    build_plan_review,
    generate_plan,
    plan_has_file_actions,
)
from uiforgemax.state import Stage, Status
from uiforgemax.stages.runner import _gate_plan, _implement, _plan_review
from uiforgemax.tools import approvals
from uiforgemax.tools.context import ToolContext


def _ctx_with_run(tmp: Path) -> tuple[ToolContext, str, Path]:
    cfg = Config(runs_root=str(tmp / "runs"))
    ctx = ToolContext.from_config(cfg)
    run_id = "empty-plan-run"
    (tmp / "proj").mkdir(exist_ok=True)
    state = ctx.store.create(
        run_id=run_id,
        project_root=str(tmp / "proj"),
        policy="balanced",
    )
    state.current_stage = Stage.PLAN_REVIEW
    state.status = Status.PLAN_READY
    ctx.store.save(state)
    return ctx, run_id, ctx.store.run_dir(run_id)


def test_build_plan_review_forces_revise_when_empty():
    review = build_plan_review({"create": [], "modify": [], "acceptanceMappings": []})
    assert review["verdict"] == "revise"
    assert EMPTY_PLAN_BLOCKER in review["blockers"]
    assert EMPTY_PLAN_BLOCKER in review["requiredPlanChanges"]


def test_build_plan_review_passes_with_file_actions():
    review = build_plan_review(
        {
            "create": [
                {
                    "path": "src/App.tsx",
                    "purpose": "app shell",
                    "changeSummary": "create App component",
                }
            ],
            "modify": [],
            "acceptanceMappings": [{"acId": "AC1"}],
        },
        requirements={"acceptanceCriteria": [{"id": "AC1"}]},
    )
    assert review["verdict"] == "pass"
    assert not review["blockers"]
    assert review.get("ideApply") is True


def test_build_plan_review_passes_intent_only_without_content():
    """Path+purpose is enough — IDE writes bodies after approval."""
    review = build_plan_review(
        {
            "create": [],
            "modify": [{"path": "src/App.tsx", "purpose": "dark mode"}],
            "acceptanceMappings": [],
        }
    )
    assert review["verdict"] == "pass"
    assert review["missingContentPaths"] == ["src/App.tsx"]
    assert review.get("ideApply") is True
    assert not any("intent" in b.lower() for b in review["blockers"])


def test_generate_plan_empty_after_sanitize_revises(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    (run_dir / "graph").mkdir()
    # Only non-implementation paths — sanitize drops them → empty plan.
    req_map = {
        "create": [],
        "modify": [{"path": "README.md", "purpose": "docs"}],
        "acceptanceMappings": [],
        "summary": "noop",
    }
    (run_dir / "graph" / "requirement-map.json").write_text(
        json.dumps(req_map), encoding="utf-8"
    )
    plan, review = generate_plan(
        run_dir,
        {"acceptanceCriteria": [], "summary": "noop"},
        req_map,
        {"gate1Required": False},
    )
    assert not plan_has_file_actions(plan)
    assert review["verdict"] == "revise"
    assert EMPTY_PLAN_BLOCKER in review["requiredPlanChanges"]


def test_plan_review_and_gate_block_empty_plan():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ctx, run_id, run_dir = _ctx_with_run(tmp)
        plan = {"create": [], "modify": [], "blockers": [EMPTY_PLAN_BLOCKER]}
        review = {
            "verdict": "pass",  # stale pass must still be blocked
            "requiredPlanChanges": [],
            "blockers": [],
        }
        (run_dir / "plans" / "implementation-plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        (run_dir / "plans" / "plan-review.json").write_text(
            json.dumps(review), encoding="utf-8"
        )
        state = ctx.store.load(run_id)
        result = _plan_review(ctx, state)
        assert result.stop is True
        assert state.status == Status.BLOCKED
        ctx.store.save(state)

        state = ctx.store.load(run_id)
        state.status = Status.PLAN_REVIEWED
        state.current_stage = Stage.GATE_PLAN
        gate = _gate_plan(ctx, state)
        assert gate.stop is True
        assert state.status == Status.BLOCKED
        assert "nothing to implement" in gate.message


def test_approve_plan_rejects_empty():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ctx, run_id, run_dir = _ctx_with_run(tmp)
        (run_dir / "plans" / "implementation-plan.json").write_text(
            json.dumps({"create": [], "modify": []}), encoding="utf-8"
        )
        state = ctx.store.load(run_id)
        state.status = Status.AWAITING_PLAN_APPROVAL
        state.current_stage = Stage.GATE_PLAN
        ctx.store.save(state)

        raw = approvals.approve_plan(ctx, run_id, by="tester")
        data = json.loads(raw)
        assert data.get("stop") is True
        assert "BLOCKED" in data.get("message", "")
        state = ctx.store.load(run_id)
        assert state.status == Status.BLOCKED
        assert not state.approvals.plan.approved
        assert not (run_dir / "plans" / "approved-plan.json").exists()


def test_implement_fails_on_zero_actions():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ctx, run_id, run_dir = _ctx_with_run(tmp)
        (run_dir / "plans" / "approved-plan.json").write_text(
            json.dumps({"create": [], "modify": [], "executionOrder": []}),
            encoding="utf-8",
        )
        state = ctx.store.load(run_id)
        state.status = Status.IMPLEMENTING
        state.current_stage = Stage.IMPLEMENT
        state.approvals.plan.approved = True
        ctx.store.save(state)

        result = _implement(ctx, state)
        assert result.stop is True
        assert state.status == Status.FAILED
        assert "0 create/modify" in result.message


def test_test_stage_blocked_when_implement_wrote_nothing():
    from uiforgemax.stages.runner import _test

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ctx, run_id, run_dir = _ctx_with_run(tmp)
        (run_dir / "implementation" / "diff-summary.json").write_text(
            json.dumps({"fileCount": 0, "filesChanged": [], "skipped": []}),
            encoding="utf-8",
        )
        (run_dir / "plans" / "approved-plan.json").write_text(
            json.dumps({"create": [], "modify": []}),
            encoding="utf-8",
        )
        state = ctx.store.load(run_id)
        state.status = Status.TESTING
        state.current_stage = Stage.TEST
        state.approvals.plan.approved = True
        ctx.store.save(state)

        result = _test(ctx, state)
        assert result.stop is True
        assert state.status == Status.FAILED
        assert "0 files changed" in result.message
