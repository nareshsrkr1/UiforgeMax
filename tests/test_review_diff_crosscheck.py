"""Deterministic cross-check: POST_IMPLEMENT_REVIEW's passesReview/planCoverage
are entirely model self-reported. _implement() must not trust a review that
claims full coverage when diff-summary.json (MCP's own record of what was
actually written) shows a planned path was genuinely never written.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.planning import plan_paths_missing_from_diff
from uiforgemax.state import Stage, Status
from uiforgemax.stages.runner import _implement
from uiforgemax.tools.context import ToolContext


def _ctx_with_run(tmp: Path) -> tuple[ToolContext, str, Path]:
    cfg = Config(runs_root=str(tmp / "runs"))
    ctx = ToolContext.from_config(cfg)
    run_id = "review-crosscheck-run"
    (tmp / "proj").mkdir(exist_ok=True)
    state = ctx.store.create(run_id=run_id, project_root=str(tmp / "proj"), policy="balanced")
    state.current_stage = Stage.IMPLEMENT
    state.status = Status.IMPLEMENTING
    state.approvals.plan.approved = True
    ctx.store.save(state)
    return ctx, run_id, ctx.store.run_dir(run_id)


# --- Pure function ---

def test_missing_path_detected():
    plan = {"create": [{"path": "a.ts"}], "modify": [{"path": "b.ts"}]}
    diff = {"filesChanged": ["a.ts"]}
    assert plan_paths_missing_from_diff(plan, diff) == ["b.ts"]


def test_fully_covered_returns_empty():
    plan = {"create": [{"path": "a.ts"}], "modify": [{"path": "b.ts"}]}
    diff = {"filesChanged": ["a.ts", "b.ts"]}
    assert plan_paths_missing_from_diff(plan, diff) == []


def test_no_plan_or_diff_returns_empty():
    assert plan_paths_missing_from_diff(None, {"filesChanged": []}) == []
    assert plan_paths_missing_from_diff({"create": [{"path": "a.ts"}]}, None) == []


# --- _implement() wiring ---

def test_implement_overrides_false_positive_passing_review(tmp_path: Path):
    """The review says passesReview=true with zero issues, but diff-summary proves
    a planned path was never written — must be overridden to failed and rewound."""
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)

    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps(
            {
                "create": [{"path": "src/Feature.tsx", "content": "export const x = 1;\n"}],
                "modify": [],
                "executionOrder": ["src/Feature.tsx"],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    # Ground truth: Feature.tsx was NEVER actually written.
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 0, "filesChanged": [], "skipped": []}), encoding="utf-8"
    )
    # The review dishonestly (or mistakenly) claims full success.
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps({"passesReview": True, "issues": [], "planCoverage": {"missing": []}}),
        encoding="utf-8",
    )

    state = ctx.store.load(run_id)
    result = _implement(ctx, state)

    assert result.stop is True
    review = result.extra["postImplementReview"]
    assert review["passesReview"] is False
    assert review["realMissingPaths"] == ["src/Feature.tsx"]
    assert "src/Feature.tsx" in review["planCoverage"]["missing"]
    assert any(i.get("file") == "src/Feature.tsx" for i in review["issues"])
    assert state.status == Status.PLAN_READY
    assert state.current_stage == Stage.PLAN
    assert state.approvals.plan.approved is False
    # Approved plan must be cleared so PLAN_REFINEMENT re-runs with fixes.
    assert not (run_dir / "plans" / "approved-plan.json").exists()


def test_implement_trusts_honest_failing_review(tmp_path: Path):
    """When the review honestly reports passesReview=false for a real logic issue
    (not a coverage gap), the existing rewind path still applies unmodified."""
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)

    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps(
            {
                "create": [{"path": "src/Feature.tsx", "content": "export const x = 1;\n"}],
                "modify": [],
                "executionOrder": ["src/Feature.tsx"],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 1, "filesChanged": ["src/Feature.tsx"], "skipped": []}),
        encoding="utf-8",
    )
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps(
            {
                "passesReview": False,
                "issues": [{"severity": "critical", "file": "src/Feature.tsx", "issue": "broken import"}],
                "planCoverage": {"missing": []},
            }
        ),
        encoding="utf-8",
    )

    state = ctx.store.load(run_id)
    result = _implement(ctx, state)

    assert result.stop is True
    assert "broken import" in result.message


def test_implement_passes_when_coverage_is_genuinely_complete(tmp_path: Path):
    ctx, run_id, run_dir = _ctx_with_run(tmp_path)

    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps(
            {
                "create": [{"path": "src/Feature.tsx", "content": "export const x = 1;\n"}],
                "modify": [],
                "executionOrder": ["src/Feature.tsx"],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "implementation").mkdir(parents=True, exist_ok=True)
    (run_dir / "implementation" / "diff-summary.json").write_text(
        json.dumps({"fileCount": 1, "filesChanged": ["src/Feature.tsx"], "skipped": []}),
        encoding="utf-8",
    )
    (run_dir / "implementation" / "post-implement-review.json").write_text(
        json.dumps({"passesReview": True, "issues": [], "planCoverage": {"missing": []}}),
        encoding="utf-8",
    )

    state = ctx.store.load(run_id)
    result = _implement(ctx, state)

    assert result.stop is False
    assert state.status == Status.IMPLEMENTING
