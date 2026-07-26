"""Regression tests for the visual-fidelity delta-loop review findings:

#1 - attempt tracking was split across two disconnected sources (model-supplied
     "_attempt" in visual-validation.json, never populated, vs "attempt" in
     visual-delta.json, which the code writes but never reads back) — the
     max-2-attempts cap could never trip, allowing an infinite PLAN<->VISUAL loop.
#2 - PLAN_REFINEMENT never read plans/visual-delta.json, and nothing enforced
     that a delta re-plan stayed scoped to the failed sub-tasks only.
#3 - answer_clarifications recorded answers to a side file but never marked
     requirement-map.json's clarifications resolved=true, so the GATE_PLAN hard
     blocker on unresolved clarifications never actually cleared.
#4a - an omitted passesReview field was silently treated as "passed" instead of
      "unverified".
#4c - flow_router required BOTH classification's runVisual guess AND a real
      visual reference to trigger VISUAL_VALIDATE, so an early wrong guess could
      skip the fidelity check entirely even with genuine HTML SoT present.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.flow_router import build_flow_plan
from uiforgemax.pipeline.planning import plan_actions_outside_delta_scope
from uiforgemax.state import RunState, Stage, Status
from uiforgemax.stages.runner import _implement, _visual_validate
from uiforgemax.tools import clarifications
from uiforgemax.tools.context import ToolContext


def _ctx_with_run(tmp: Path) -> tuple[ToolContext, str, Path]:
    cfg = Config(runs_root=str(tmp / "runs"))
    ctx = ToolContext.from_config(cfg)
    run_id = "delta-loop-run"
    (tmp / "proj").mkdir(exist_ok=True)
    state = ctx.store.create(run_id=run_id, project_root=str(tmp / "proj"), policy="balanced")
    ctx.store.save(state)
    return ctx, run_id, ctx.store.run_dir(run_id)


def _setup_visual_stage(run_dir: Path, *, fidelity_json: dict, delta_json: dict | None = None) -> None:
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "visual-spec.json").write_text(json.dumps({"htmlDerived": True}), encoding="utf-8")
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": [], "subtaskPlan": {"subtasks": []}}), encoding="utf-8"
    )
    (run_dir / "implementation" / "visual-validation.json").write_text(
        json.dumps(fidelity_json), encoding="utf-8"
    )
    if delta_json is not None:
        (run_dir / "plans" / "visual-delta.json").write_text(json.dumps(delta_json), encoding="utf-8")


# --- #1: attempt tracking is code-owned and actually advances ---

def test_first_failure_rewinds_and_stamps_attempt_2(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "1")
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    _setup_visual_stage(
        run_dir,
        fidelity_json={
            "passesVisualGate": False,
            "overallFidelity": 0.4,
            "subtaskResults": [{"subtaskId": "ST-1", "needsReimplementation": True, "fidelityScore": 0.4}],
        },
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.VISUAL_VALIDATE
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _visual_validate(ctx, state)

    assert result.stop is True
    assert state.current_stage == Stage.PLAN
    delta = json.loads((run_dir / "plans" / "visual-delta.json").read_text(encoding="utf-8"))
    assert delta["attempt"] == 2  # advanced from 1 (no prior delta file) to 2


def test_second_failure_reads_attempt_from_delta_file_and_stops_looping(tmp_path: Path, monkeypatch):
    """This is the exact bug: a SECOND cycle's visual-validation.json still has
    no attempt info the model reliably sets — the fix must read the current
    attempt count from the prior visual-delta.json (attempt=2), not default to 1
    forever."""
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "1")
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    _setup_visual_stage(
        run_dir,
        fidelity_json={
            "passesVisualGate": False,
            "overallFidelity": 0.5,
            "subtaskResults": [{"subtaskId": "ST-1", "needsReimplementation": True, "fidelityScore": 0.5}],
        },
        delta_json={"version": 1, "attempt": 2, "failedSubtasks": [], "source": "visual_validation"},
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.VISUAL_VALIDATE
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _visual_validate(ctx, state)

    # Must proceed with a warning, NOT rewind again — this is what "max 2
    # attempts" is supposed to mean, and previously never triggered.
    assert result.stop is False
    assert state.status == Status.VISUAL_VALIDATED
    assert "2 attempts" in result.message


# --- #2: PLAN_REFINEMENT must scope a delta re-plan to failed sub-tasks only ---

def test_delta_scope_flags_actions_missing_subtask_id():
    plan = {"create": [{"path": "a.ts", "subtaskId": "ST-1"}], "modify": [{"path": "b.ts"}]}
    delta = {"failedSubtasks": [{"subtaskId": "ST-1"}]}
    assert plan_actions_outside_delta_scope(plan, delta) == ["b.ts"]


def test_delta_scope_flags_actions_for_passed_subtask():
    plan = {"create": [{"path": "a.ts", "subtaskId": "ST-2"}]}
    delta = {"failedSubtasks": [{"subtaskId": "ST-1"}]}
    assert plan_actions_outside_delta_scope(plan, delta) == ["a.ts"]


def test_delta_scope_passes_when_all_actions_tagged_to_failed_ids():
    plan = {
        "create": [{"path": "a.ts", "subtaskId": "ST-1"}],
        "modify": [{"path": "b.ts", "subtaskId": "ST-2"}],
    }
    delta = {"failedSubtasks": [{"subtaskId": "ST-1"}, {"subtaskId": "ST-2"}]}
    assert plan_actions_outside_delta_scope(plan, delta) == []


def test_delta_scope_noop_without_delta_file():
    plan = {"create": [{"path": "a.ts"}]}
    assert plan_actions_outside_delta_scope(plan, None) == []


def test_submit_mediation_blocks_out_of_scope_delta_plan(tmp_path: Path):
    import tempfile as _tempfile

    from uiforgemax.tools import lifecycle, mediation, preflight

    runs = _tempfile.mkdtemp()
    ctx = ToolContext.from_config(Config(runs_root=runs))
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x = 1\n", encoding="utf-8")

    preflight.preflight(ctx, project_root=str(project))
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]
    run_dir = ctx.store.run_dir(run_id)

    (run_dir / "plans").mkdir(parents=True, exist_ok=True)
    (run_dir / "plans" / "visual-delta.json").write_text(
        json.dumps({"failedSubtasks": [{"subtaskId": "ST-1"}]}), encoding="utf-8"
    )

    state = ctx.store.load(run_id)
    state.current_stage = Stage.PLAN
    state.status = Status.AWAITING_MEDIATION
    state.artifacts["pendingMediation"] = "7_plan::PLAN_REFINEMENT"
    ctx.store.save(state)

    payload = json.dumps(
        {
            "summary": "delta fix",
            "create": [
                {
                    "path": "unrelated.py",
                    "purpose": "out of scope helper",
                    "changeSummary": "should be rejected by delta scope",
                    "subtaskId": "ST-99",
                }
            ],
            "modify": [],
        }
    )
    out = json.loads(mediation.submit_mediation(ctx, run_id, "7_plan::PLAN_REFINEMENT", payload))
    assert out["stop"] is True
    assert "DELTA re-plan scoped" in out["message"]
    assert "unrelated.py" in out["message"]


# --- #3: answer_clarifications must clear the requirement-map hard blocker ---

def test_answer_clarifications_marks_resolved_by_id(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "graph").mkdir(parents=True, exist_ok=True)
    (run_dir / "graph" / "requirement-map.json").write_text(
        json.dumps(
            {
                "clarifications": [
                    {"id": "CL-1", "question": "Which theme?", "resolved": False},
                    {"id": "CL-2", "question": "Which API?", "resolved": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    out = json.loads(
        clarifications.answer_clarifications(ctx, run_id, json.dumps({"CL-1": "dark theme"}))
    )
    assert "1 marked resolved" in out["message"]
    req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in req_map["clarifications"]}
    assert by_id["CL-1"]["resolved"] is True
    assert by_id["CL-1"]["answer"] == "dark theme"
    assert by_id["CL-2"]["resolved"] is False  # untouched — wasn't answered


def test_answer_clarifications_raw_text_resolves_all_open(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "graph").mkdir(parents=True, exist_ok=True)
    (run_dir / "graph" / "requirement-map.json").write_text(
        json.dumps(
            {"clarifications": [{"id": "CL-1", "question": "Which theme?", "resolved": False}]}
        ),
        encoding="utf-8",
    )
    clarifications.answer_clarifications(ctx, run_id, "use the dark theme everywhere")
    req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
    assert req_map["clarifications"][0]["resolved"] is True


def test_answer_clarifications_hard_blocker_actually_clears(tmp_path: Path):
    """End-to-end: build_plan_review's own blocker check must see resolved=true
    after answer_clarifications, not just the side-file record."""
    from uiforgemax.pipeline.planning import build_plan_review

    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "graph").mkdir(parents=True, exist_ok=True)
    req_map_path = run_dir / "graph" / "requirement-map.json"
    req_map_path.write_text(
        json.dumps({"clarifications": [{"id": "CL-1", "question": "Which theme?", "resolved": False}]}),
        encoding="utf-8",
    )
    plan = {
        "create": [{"path": "a.ts", "content": "export const x = 1;\n"}],
        "modify": [],
        "acceptanceMappings": [],
    }
    before = build_plan_review(plan, req_map=json.loads(req_map_path.read_text(encoding="utf-8")))
    assert any("Unresolved clarification" in b for b in before["blockers"])

    clarifications.answer_clarifications(ctx, run_id, json.dumps({"CL-1": "dark theme"}))

    after = build_plan_review(plan, req_map=json.loads(req_map_path.read_text(encoding="utf-8")))
    assert not any("Unresolved clarification" in b for b in after["blockers"])


# --- #4a: omitted passesReview must not silently pass ---

def test_implement_treats_omitted_passes_review_as_failed(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [{"path": "a.ts", "content": "x\n"}], "modify": [], "executionOrder": ["a.ts"]}),
        encoding="utf-8",
    )
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 1, "filesChanged": ["a.ts"], "skipped": []}), encoding="utf-8"
    )
    # No passesReview key at all — the exact omission case.
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps({"issues": []}), encoding="utf-8"
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.IMPLEMENT
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _implement(ctx, state)

    assert result.stop is True
    assert state.status == Status.PLAN_READY  # rewound, not silently continued
    assert "omitted 'passesReview'" in result.extra["postImplementReview"]["issues"][0]["issue"]


def test_implement_still_passes_on_explicit_true(tmp_path: Path):
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
        json.dumps({"passesReview": True, "issues": []}), encoding="utf-8"
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.IMPLEMENT
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _implement(ctx, state)
    assert result.stop is False
    assert state.status == Status.IMPLEMENTING


# --- #4c: a real visual reference must not be skippable by a runVisual guess ---

def _state(tmp: Path) -> RunState:
    return RunState(run_id="flow-test", project_root=str(tmp), status=Status.CLASSIFIED)


def test_visual_validate_stage_active_when_html_ref_present_despite_run_visual_false(tmp_path: Path):
    classification = {
        "requestType": "enhancement",
        "surface": "ui_only",
        "runVisual": False,  # model's early (wrong) guess
    }
    signals = {"inputModes": ["html"], "hasHtml": True, "projectRoot": {"empty": False}}
    flow = build_flow_plan(classification, signals, _state(tmp_path))
    assert "9.5_visual_validate" in flow["activeStages"]


def test_visual_validate_stage_skipped_when_truly_no_visual_ref(tmp_path: Path):
    classification = {"requestType": "enhancement", "surface": "api_only", "runVisual": False}
    signals = {"inputModes": [], "projectRoot": {"empty": False}}
    flow = build_flow_plan(classification, signals, _state(tmp_path))
    assert "9.5_visual_validate" in flow["skippedStages"]
