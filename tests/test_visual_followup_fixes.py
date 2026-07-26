"""Regression tests for the second review pass's 5 follow-up findings:

1. VISUAL_INTERPRETATION was gated on classification's runVisual guess — a real
   HTML/wireframe SoT with runVisual=false never got model interpretation.
2. Delta-scope enforcement only lived in submit_mediation, not _gate_plan, so
   UIFORGEMAX_SKIP_MEDIATION=1 could approve an out-of-scope delta plan.
3. visual_sot.py listed "design_notes" in kinds[] but never counted it toward
   hasVisualRef, so design-notes-only intake activated no visual stage.
4. prepare_delta_reimplementation read "fidelityScore" but the mediation schema
   asks for "fidelity" — scores silently defaulted to 0.
5. A rubber-stamped passesReview=true with critical issues[] still counted as
   passing (only the omitted-field case was covered before).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.model_mediation.registry import pending_mediations
from uiforgemax.pipeline.visual_sot import detect_visual_references
from uiforgemax.pipeline.visual_validate import prepare_delta_reimplementation
from uiforgemax.state import RunState, Stage, Status
from uiforgemax.stages.runner import _gate_plan, _implement
from uiforgemax.tools.context import ToolContext


def _ctx_with_run(tmp: Path) -> tuple[ToolContext, str, Path]:
    cfg = Config(runs_root=str(tmp / "runs"))
    ctx = ToolContext.from_config(cfg)
    run_id = "followup-run"
    (tmp / "proj").mkdir(exist_ok=True)
    state = ctx.store.create(run_id=run_id, project_root=str(tmp / "proj"), policy="balanced")
    ctx.store.save(state)
    return ctx, run_id, ctx.store.run_dir(run_id)


# --- #1: VISUAL_INTERPRETATION must trigger on hasVisualRef, ignoring runVisual ---

def test_visual_interpretation_pending_despite_run_visual_false(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "inputs").mkdir(parents=True)
    (run_dir / "inputs" / "page.html").write_text("<html></html>", encoding="utf-8")
    state = RunState(run_id="x", project_root=str(tmp_path), status=Status.CLASSIFIED)
    state.inputs["modes"] = ["html"]
    (run_dir / "run-flow.json").write_text(
        json.dumps({"flags": {"runVisual": False}, "activeStages": []}), encoding="utf-8"
    )
    (run_dir / "requirements.normalized.json").write_text(
        json.dumps({"summary": "test", "acceptanceCriteria": []}), encoding="utf-8"
    )
    (run_dir / "request-classification.json").write_text(json.dumps({"surface": "ui_only"}), encoding="utf-8")

    pending = pending_mediations(Stage.NORMALIZE, run_dir, state)
    kinds = [k.value for _, k in pending]
    # Visual SoT folds into REQUIREMENT_ANALYSIS (UNDERSTAND pass).
    assert "REQUIREMENT_ANALYSIS" in kinds
    assert "VISUAL_INTERPRETATION" not in kinds


# --- #2: delta scope enforced at _gate_plan too (SKIP_MEDIATION safety net) ---

def test_gate_plan_blocks_out_of_scope_delta_plan(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )
    (run_dir / "plans" / "implementation-plan.json").write_text(
        json.dumps(
            {
                "create": [
                    {
                        "path": "unrelated.py",
                        "purpose": "out of scope",
                        "changeSummary": "helper",
                        "subtaskId": "ST-99",
                    }
                ],
                "modify": [],
            }
        ),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.GATE_PLAN
    state.status = Status.PLAN_READY
    ctx.store.save(state)

    result = _gate_plan(ctx, state)
    assert result.stop is True
    assert "DELTA re-plan scoped" in result.message
    assert state.status == Status.BLOCKED


def test_gate_plan_passes_in_scope_delta_plan(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )
    (run_dir / "plans" / "implementation-plan.json").write_text(
        json.dumps(
            {
                "create": [
                    {
                        "path": "fixed.py",
                        "purpose": "fix failed subtask",
                        "changeSummary": "align with visual delta",
                        "subtaskId": "ST-1",
                    }
                ],
                "modify": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "plans" / "plan-review.json").write_text(
        json.dumps({"verdict": "pass", "blockers": [], "risks": []}), encoding="utf-8"
    )
    (run_dir / "requirements.normalized.json").write_text(
        json.dumps({"summary": "t", "acceptanceCriteria": []}), encoding="utf-8"
    )
    (run_dir / "plans" / "understanding-approval.json").write_text("{}", encoding="utf-8")
    state = ctx.store.load(run_id)
    state.current_stage = Stage.GATE_PLAN
    state.status = Status.PLAN_READY
    ctx.store.save(state)

    result = _gate_plan(ctx, state)
    assert state.status != Status.BLOCKED


# --- #3: design_notes must count toward hasVisualRef, not just kinds[] ---

def test_design_notes_only_intake_sets_has_visual_ref(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "inputs" / "attachments").mkdir(parents=True)
    notes_path = run_dir / "inputs" / "attachments" / "design-notes.md"
    notes_path.write_text("# Design notes\nUse navy/gold theme.\n", encoding="utf-8")
    (run_dir / "inputs" / "attachments.json").write_text(
        json.dumps(
            {
                "attachments": [
                    {
                        "filename": "design-notes.md",
                        "role": "design_notes",
                        "storedAs": "design-notes.md",
                        "path": "inputs/attachments/design-notes.md",
                        "sourceOfTruth": True,
                    }
                ],
                "designNotes": ["inputs/attachments/design-notes.md"],
            }
        ),
        encoding="utf-8",
    )
    ref = detect_visual_references(run_dir, None)
    assert "design_notes" in ref["kinds"]
    assert ref["hasVisualRef"] is True  # previously always False for this case


# --- #4: fidelity field name — must not silently default to 0 ---

def test_prepare_delta_reads_fidelity_field_not_only_fidelityscore(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    state = RunState(run_id="x", project_root=str(tmp_path), status=Status.IMPLEMENTING)
    failed = [{"subtaskId": "ST-1", "fidelity": 0.42, "issues": [], "affectedFiles": []}]
    delta = prepare_delta_reimplementation(run_dir, state, failed, attempt=1)
    assert delta["failedSubtasks"][0]["fidelityScore"] == 0.42


def test_prepare_delta_still_honors_fidelityscore_if_present(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    state = RunState(run_id="x", project_root=str(tmp_path), status=Status.IMPLEMENTING)
    failed = [{"subtaskId": "ST-1", "fidelityScore": 0.9, "fidelity": 0.1, "issues": [], "affectedFiles": []}]
    delta = prepare_delta_reimplementation(run_dir, state, failed, attempt=1)
    assert delta["failedSubtasks"][0]["fidelityScore"] == 0.9  # explicit fidelityScore wins


# --- #5: passesReview=true must not override the model's own critical issues ---

def test_implement_overrides_rubberstamped_pass_with_critical_issue(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [{"path": "a.ts", "content": "x\n"}], "modify": [], "executionOrder": ["a.ts"]}),
        encoding="utf-8",
    )
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 1, "filesChanged": ["a.ts"], "skipped": []}), encoding="utf-8"
    )
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps(
            {
                "passesReview": True,  # rubber-stamped
                "issues": [{"severity": "critical", "file": "a.ts", "issue": "wrong button label vs HTML SoT"}],
            }
        ),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.IMPLEMENT
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _implement(ctx, state)
    assert result.stop is True
    assert state.status == Status.PLAN_READY  # rewound despite passesReview=true


def test_implement_still_passes_true_with_no_critical_issues(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [{"path": "a.ts", "content": "x\n"}], "modify": [], "executionOrder": ["a.ts"]}),
        encoding="utf-8",
    )
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 1, "filesChanged": ["a.ts"], "skipped": []}), encoding="utf-8"
    )
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps({"passesReview": True, "issues": [{"severity": "minor", "file": "a.ts", "issue": "nit"}]}),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.IMPLEMENT
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _implement(ctx, state)
    assert result.stop is False
    assert state.status == Status.IMPLEMENTING
