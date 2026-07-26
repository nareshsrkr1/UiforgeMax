"""Deterministic no-op detection: a 'modify' action whose content is identical to
the original file must be caught in code, not left to model self-grading alone.

Weak point this closes: action_has_writable_body() only checks content is
non-empty — it can't tell a genuine edit from the model returning the file
unchanged. POST_IMPLEMENT_REVIEW's passesReview gate is the only other check,
and it's fully model self-graded (nothing in code cross-checks its claims).
plan_actions_noop_modifies() is the deterministic backstop between those two.
"""

from __future__ import annotations

import json

from uiforgemax.pipeline.planning import plan_actions_noop_modifies


def _snapshot(path: str, content: str, *, root: str = "default", **overrides) -> dict:
    entry = {"path": path, "root": root, "exists": True, "content": content}
    entry.update(overrides)
    return {"files": {f"{root}:{path}": entry}}


def test_identical_content_flagged_as_noop():
    plan = {"modify": [{"path": "a.ts", "content": "export const x = 1;\n"}]}
    snapshots = _snapshot("a.ts", "export const x = 1;\n")
    assert plan_actions_noop_modifies(plan, snapshots) == ["a.ts"]


def test_genuinely_different_content_passes():
    plan = {"modify": [{"path": "a.ts", "content": "export const x = 2;\n"}]}
    snapshots = _snapshot("a.ts", "export const x = 1;\n")
    assert plan_actions_noop_modifies(plan, snapshots) == []


def test_whitespace_only_difference_still_flagged():
    """Trailing whitespace / line-ending differences alone are not a real edit."""
    plan = {"modify": [{"path": "a.ts", "content": "export const x = 1;   \r\n"}]}
    snapshots = _snapshot("a.ts", "export const x = 1;\n")
    assert plan_actions_noop_modifies(plan, snapshots) == ["a.ts"]


def test_missing_content_not_double_flagged_here():
    """Empty/missing content is the missing-content channel's job, not this one."""
    plan = {"modify": [{"path": "a.ts", "content": ""}]}
    snapshots = _snapshot("a.ts", "export const x = 1;\n")
    assert plan_actions_noop_modifies(plan, snapshots) == []


def test_new_file_snapshot_not_flagged():
    """exists=False (genuinely new path) has nothing to compare against — skip."""
    plan = {"modify": [{"path": "a.ts", "content": "export const x = 1;\n"}]}
    snapshots = {"files": {"default:a.ts": {"path": "a.ts", "root": "default", "exists": False}}}
    assert plan_actions_noop_modifies(plan, snapshots) == []


def test_binary_snapshot_not_flagged():
    plan = {"modify": [{"path": "logo.png", "content": "not real png bytes"}]}
    snapshots = _snapshot("logo.png", "", binary=True)
    assert plan_actions_noop_modifies(plan, snapshots) == []


def test_truncated_snapshot_not_flagged():
    plan = {"modify": [{"path": "big.ts", "content": "same head..."}]}
    snapshots = _snapshot("big.ts", "same head...", truncated=True)
    assert plan_actions_noop_modifies(plan, snapshots) == []


def test_no_snapshots_at_all_returns_empty():
    plan = {"modify": [{"path": "a.ts", "content": "x"}]}
    assert plan_actions_noop_modifies(plan, None) == []
    assert plan_actions_noop_modifies(plan, {}) == []


def test_create_actions_never_checked():
    """No-op detection only applies to modify — create has no 'original' to compare."""
    plan = {"create": [{"path": "new.ts", "content": "export const x = 1;\n"}]}
    snapshots = _snapshot("new.ts", "export const x = 1;\n")
    assert plan_actions_noop_modifies(plan, snapshots) == []


def test_multi_root_action_matches_correct_root_snapshot():
    plan = {"modify": [{"path": "a.ts", "root": "backend", "content": "same\n"}]}
    snapshots = {
        "files": {
            "default:a.ts": {"path": "a.ts", "root": "default", "exists": True, "content": "different\n"},
            "backend:a.ts": {"path": "a.ts", "root": "backend", "exists": True, "content": "same\n"},
        }
    }
    assert plan_actions_noop_modifies(plan, snapshots) == ["a.ts"]


def test_multiple_noop_paths_all_reported():
    plan = {
        "modify": [
            {"path": "a.ts", "content": "same a\n"},
            {"path": "b.ts", "content": "changed b\n"},
            {"path": "c.ts", "content": "same c\n"},
        ]
    }
    snapshots = {
        "files": {
            "default:a.ts": {"path": "a.ts", "root": "default", "exists": True, "content": "same a\n"},
            "default:b.ts": {"path": "b.ts", "root": "default", "exists": True, "content": "original b\n"},
            "default:c.ts": {"path": "c.ts", "root": "default", "exists": True, "content": "same c\n"},
        }
    }
    assert plan_actions_noop_modifies(plan, snapshots) == ["a.ts", "c.ts"]


# --- Wiring into submit_mediation ---

def test_submit_mediation_blocks_noop_plan_refinement(tmp_path):
    from uiforgemax.config import Config
    from uiforgemax.tools import ToolContext, inputs, lifecycle, mediation
    from uiforgemax.state import Stage, Status

    import tempfile

    runs = tempfile.mkdtemp()
    ctx = ToolContext.from_config(Config(runs_root=runs))
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x = 1\n", encoding="utf-8")

    from uiforgemax.tools import preflight

    preflight.preflight(ctx, project_root=str(project))
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]

    run_dir = ctx.store.run_dir(run_id)
    (run_dir / "graph").mkdir(parents=True, exist_ok=True)
    (run_dir / "graph" / "source-snapshots.json").write_text(
        json.dumps(_snapshot("main.py", "x = 1\n")), encoding="utf-8"
    )

    state = ctx.store.load(run_id)
    state.current_stage = Stage.PLAN
    state.status = Status.AWAITING_MEDIATION
    state.artifacts["pendingMediation"] = "7_plan::PLAN_REFINEMENT"
    ctx.store.save(state)

    payload = json.dumps(
        {
            "summary": "test",
            "create": [],
            "modify": [{"path": "main.py", "content": "x = 1\n", "purpose": "no-op"}],
        }
    )
    out = json.loads(
        mediation.submit_mediation(ctx, run_id, "7_plan::PLAN_REFINEMENT", payload)
    )
    assert out["stop"] is True
    assert "IDENTICAL to the original" in out["message"]
    assert "main.py" in out["message"]
