"""Regression tests for external review findings P1/P2/P3:

P1 - visual-validation rewind must clear the stale post-implement-review.json,
     otherwise the next _implement() cycle short-circuits on the OLD passed
     review and never calls apply_plan() again for the delta fixes.
P2 - validate_test_generation must accept idiomatic Go tests (t.Errorf/t.Fatal/
     t.Run have no "assert" substring at all) instead of rejecting them as stubs.
P3 - the noop-modify / placeholder-create deterministic backstops must also
     apply at _gate_plan (the SKIP_MEDIATION / deterministic-plan path), not
     only inside submit_mediation's PLAN_REFINEMENT handling.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.testing import validate_test_generation
from uiforgemax.state import Stage, Status
from uiforgemax.stages.runner import _gate_plan, _visual_validate
from uiforgemax.tools.context import ToolContext


def _ctx_with_run(tmp: Path) -> tuple[ToolContext, str, Path]:
    cfg = Config(runs_root=str(tmp / "runs"))
    ctx = ToolContext.from_config(cfg)
    run_id = "review-fixes-run"
    (tmp / "proj").mkdir(exist_ok=True)
    state = ctx.store.create(run_id=run_id, project_root=str(tmp / "proj"), policy="balanced")
    ctx.store.save(state)
    return ctx, run_id, ctx.store.run_dir(run_id)


# --- P1: visual rewind clears stale post-implement-review.json ---

def test_visual_rewind_archives_stale_post_implement_review(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "1")
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "visual-spec.json").write_text(json.dumps({"htmlDerived": True}), encoding="utf-8")

    # A PASSED review from the first implement cycle — this is exactly what
    # would make the next _implement() call short-circuit without re-applying.
    review_path = run_dir / "implementation" / "post-implement-review.json"
    review_path.write_text(json.dumps({"passesReview": True, "issues": []}), encoding="utf-8")

    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": [], "subtaskPlan": {"subtasks": []}}),
        encoding="utf-8",
    )
    (run_dir / "implementation" / "visual-validation.json").write_text(
        json.dumps(
            {
                "passesVisualGate": False,
                "overallFidelity": 0.4,
                "subtaskResults": [
                    {"subtaskId": "ST-1", "needsReimplementation": True, "fidelity": 0.4}
                ],
            }
        ),
        encoding="utf-8",
    )

    state = ctx.store.load(run_id)
    state.current_stage = Stage.VISUAL_VALIDATE
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _visual_validate(ctx, state)

    assert result.stop is True
    assert result.extra["deltaReimplementation"] is True
    # The stale PASSED review must be gone from its original path...
    assert not review_path.exists()
    # ...archived, not silently deleted with no trace.
    assert (run_dir / "implementation" / "post-implement-review.superseded-by-visual.json").exists()
    assert state.current_stage == Stage.PLAN
    assert state.status == Status.PLAN_READY
    assert state.approvals.plan.approved is False


def test_visual_rewind_noop_when_no_review_exists(tmp_path: Path, monkeypatch):
    """Must not error when there's nothing to clean up."""
    monkeypatch.setenv("UIFORGEMAX_SKIP_MEDIATION", "1")
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "visual-spec.json").write_text(json.dumps({"htmlDerived": True}), encoding="utf-8")
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": [], "subtaskPlan": {"subtasks": []}}),
        encoding="utf-8",
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
    state.approvals.plan.approved = True
    ctx.store.save(state)

    result = _visual_validate(ctx, state)
    assert result.stop is True  # still rewinds normally, just nothing to archive


# --- P2: Go-style tests must not be rejected as stubs ---

def test_go_style_test_accepted(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    go_test = (
        "package feature_test\n\n"
        "import \"testing\"\n\n"
        "func TestGreet(t *testing.T) {\n"
        "    got := Greet(\"world\")\n"
        "    want := \"Hello, world!\"\n"
        "    if got != want {\n"
        "        t.Errorf(\"Greet() = %q, want %q\", got, want)\n"
        "    }\n"
        "}\n"
    )
    ok, reason = validate_test_generation(
        run_dir, {"tests": [{"path": "feature_test.go", "content": go_test}], "run": ["go test ./..."]}
    )
    assert ok is True, reason


def test_go_table_driven_with_t_run_accepted(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    go_test = (
        "package feature_test\n\n"
        "import \"testing\"\n\n"
        "func TestCases(t *testing.T) {\n"
        "    t.Run(\"case1\", func(t *testing.T) {\n"
        "        if 1+1 != 2 { t.Fatal(\"math is broken\") }\n"
        "    })\n"
        "}\n"
    )
    ok, reason = validate_test_generation(
        run_dir, {"tests": [{"path": "feature_test.go", "content": go_test}], "run": ["go test ./..."]}
    )
    assert ok is True, reason


def test_phpunit_camelcase_assert_accepted(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    php_test = (
        "<?php\n"
        "use PHPUnit\\Framework\\TestCase;\n"
        "class FeatureTest extends TestCase {\n"
        "    public function testGreet() {\n"
        "        $this->assertEquals('Hello, world!', greet('world'));\n"
        "    }\n"
        "}\n"
    )
    ok, reason = validate_test_generation(
        run_dir, {"tests": [{"path": "FeatureTest.php", "content": php_test}], "run": ["phpunit"]}
    )
    assert ok is True, reason


def test_genuinely_empty_stub_still_rejected(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ok, reason = validate_test_generation(
        run_dir, {"tests": [{"path": "a.test.ts", "content": "// nothing here yet"}]}
    )
    assert ok is False


# --- P3: deterministic backstops apply at _gate_plan, not just submit_mediation ---

def test_gate_plan_blocks_missing_intent(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "implementation-plan.json").write_text(
        json.dumps({"create": [], "modify": [{"path": "a.ts"}]}),
        encoding="utf-8",
    )
    state = ctx.store.load(run_id)
    state.current_stage = Stage.GATE_PLAN
    state.status = Status.PLAN_READY
    ctx.store.save(state)

    result = _gate_plan(ctx, state)
    assert result.stop is True
    assert "intent" in result.message.lower() or "purpose" in result.message.lower()
    assert state.status == Status.BLOCKED


def test_gate_plan_opens_human_gate_for_intent_only(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "implementation-plan.json").write_text(
        json.dumps(
            {
                "create": [
                    {
                        "path": "new.ts",
                        "purpose": "new module",
                        "changeSummary": "export helper",
                    }
                ],
                "modify": [],
            }
        ),
        encoding="utf-8",
    )
    # Minimal artifacts for plan approval package.
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
    assert state.status == Status.AWAITING_PLAN_APPROVAL
    assert result.stop is True


def test_gate_plan_passes_genuine_plan(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)
    (run_dir / "plans" / "implementation-plan.json").write_text(
        json.dumps(
            {
                "create": [
                    {
                        "path": "new.ts",
                        "purpose": "real module",
                        "changeSummary": "export const real = () => 1",
                        "content": "export const real = () => 1;\n",
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
