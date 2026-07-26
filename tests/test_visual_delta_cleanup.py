"""Stale visual-delta.json must not lock later full re-plans after the cycle ends."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.visual_validate import clear_visual_delta
from uiforgemax.state import Stage, Status
from uiforgemax.stages.runner import _implement, _visual_validate
from uiforgemax.tools.approvals import _clear_approved_plan
from uiforgemax.tools.context import ToolContext


def _ctx_with_run(tmp: Path) -> tuple[ToolContext, str, Path]:
    cfg = Config(runs_root=str(tmp / "runs"))
    ctx = ToolContext.from_config(cfg)
    run_id = "delta-cleanup-run"
    (tmp / "proj").mkdir(exist_ok=True)
    state = ctx.store.create(run_id=run_id, project_root=str(tmp / "proj"), policy="balanced")
    ctx.store.save(state)
    return ctx, run_id, ctx.store.run_dir(run_id)


def test_clear_visual_delta_archives_file(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    delta = run_dir / "plans" / "visual-delta.json"
    delta.write_text(json.dumps({"attempt": 2, "failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8")
    clear_visual_delta(run_dir, reason="passed")
    assert not delta.exists()
    assert (run_dir / "plans" / "visual-delta.superseded-passed.json").exists()


def test_visual_pass_clears_stale_delta(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "1")
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "visual-spec.json").write_text(json.dumps({"htmlDerived": True}), encoding="utf-8")
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": []}), encoding="utf-8"
    )
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"attempt": 2, "failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )
    (run_dir / "implementation" / "visual-validation.json").write_text(
        json.dumps({"passesVisualGate": True, "overallFidelity": 0.95, "subtaskResults": []}),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.VISUAL_VALIDATE
    state.status = Status.IMPLEMENTING
    ctx.store.save(state)

    result = _visual_validate(ctx, state)
    assert result.stop is False
    assert not (run_dir / "plans" / "visual-delta.json").exists()
    assert (run_dir / "plans" / "visual-delta.superseded-passed.json").exists()


def test_max_attempts_clears_stale_delta(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "1")
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "visual-spec.json").write_text(json.dumps({"htmlDerived": True}), encoding="utf-8")
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": []}), encoding="utf-8"
    )
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"attempt": 2, "failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )
    (run_dir / "implementation" / "visual-validation.json").write_text(
        json.dumps(
            {
                "passesVisualGate": False,
                "overallFidelity": 0.4,
                "subtaskResults": [{"subtaskId": "ST-1", "needsReimplementation": True}],
            }
        ),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.VISUAL_VALIDATE
    state.status = Status.IMPLEMENTING
    ctx.store.save(state)

    result = _visual_validate(ctx, state)
    assert result.stop is False
    assert "2 attempts" in result.message
    assert not (run_dir / "plans" / "visual-delta.json").exists()


def test_post_implement_rewind_clears_visual_delta(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [{"path": "a.ts", "content": "x\n"}], "modify": [], "executionOrder": ["a.ts"]}),
        encoding="utf-8",
    )
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 1, "filesChanged": ["a.ts"], "skipped": []}), encoding="utf-8"
    )
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps({"passesReview": False, "issues": [{"severity": "critical", "file": "a.ts", "issue": "bad"}]}),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.IMPLEMENT
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _implement(ctx, state)
    assert result.stop is True
    assert state.status == Status.PLAN_READY
    assert not (run_dir / "plans" / "visual-delta.json").exists()


def test_clear_approved_plan_clears_visual_delta(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    (run_dir / "implementation").mkdir(parents=True)
    (run_dir / "plans" / "approved-plan.json").write_text("{}", encoding="utf-8")
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )
    _clear_approved_plan(run_dir)
    assert not (run_dir / "plans" / "approved-plan.json").exists()
    assert not (run_dir / "plans" / "visual-delta.json").exists()
