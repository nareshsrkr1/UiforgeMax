"""Deterministic placeholder-create detection: a 'create' action whose content is
a trivial stub/near-empty body must be caught in code, not left to model
self-grading (POST_IMPLEMENT_REVIEW) alone.
"""

from __future__ import annotations

import json

from uiforgemax.pipeline.planning import plan_actions_placeholder_creates


def test_near_empty_content_flagged():
    plan = {"create": [{"path": "a.ts", "content": "// x"}]}
    assert plan_actions_placeholder_creates(plan) == ["a.ts"]


def test_todo_only_flagged():
    plan = {"create": [{"path": "a.ts", "content": "// TODO: implement this component\n"}]}
    assert plan_actions_placeholder_creates(plan) == ["a.ts"]


def test_multiline_all_placeholder_flagged():
    plan = {
        "create": [
            {
                "path": "a.ts",
                "content": "// TODO: implement\n// FIXME: add real logic\n",
            }
        ]
    }
    assert plan_actions_placeholder_creates(plan) == ["a.ts"]


def test_real_implementation_passes():
    plan = {
        "create": [
            {
                "path": "a.ts",
                "content": "export interface User {\n  id: string;\n  name: string;\n}\n",
            }
        ]
    }
    assert plan_actions_placeholder_creates(plan) == []


def test_real_implementation_with_a_todo_comment_still_passes():
    """A TODO comment alongside real code is fine — only ALL-placeholder content is flagged."""
    plan = {
        "create": [
            {
                "path": "a.ts",
                "content": (
                    "// TODO: add validation later\n"
                    "export function greet(name: string): string {\n"
                    "  return `Hello, ${name}!`;\n"
                    "}\n"
                ),
            }
        ]
    }
    assert plan_actions_placeholder_creates(plan) == []


def test_missing_content_not_flagged_here():
    """Missing content is the missing-content channel's job, not this one."""
    plan = {"create": [{"path": "a.ts", "content": None}]}
    assert plan_actions_placeholder_creates(plan) == []


def test_greenfield_template_scaffold_unaffected():
    """No literal content (templateId scaffold) — nothing for this check to inspect."""
    plan = {"create": [{"path": "a.ts", "templateId": "greenfield.ui_index"}]}
    assert plan_actions_placeholder_creates(plan) == []


def test_modify_actions_never_checked():
    """Placeholder detection only applies to create — modify has its own no-op check."""
    plan = {"modify": [{"path": "a.ts", "content": "// TODO"}]}
    assert plan_actions_placeholder_creates(plan) == []


def test_multiple_placeholders_all_reported():
    plan = {
        "create": [
            {"path": "a.ts", "content": "// TODO"},
            {"path": "b.ts", "content": "export const real = () => 'implemented';\n"},
            {"path": "c.ts", "content": "TBD"},
        ]
    }
    assert plan_actions_placeholder_creates(plan) == ["a.ts", "c.ts"]


# --- Wiring into submit_mediation ---

def test_submit_mediation_blocks_placeholder_create(tmp_path):
    import tempfile

    from uiforgemax.config import Config
    from uiforgemax.state import Stage, Status
    from uiforgemax.tools import ToolContext, lifecycle, mediation, preflight

    runs = tempfile.mkdtemp()
    ctx = ToolContext.from_config(Config(runs_root=runs))
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("x = 1\n", encoding="utf-8")

    preflight.preflight(ctx, project_root=str(project))
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(project)))["runId"]

    state = ctx.store.load(run_id)
    state.current_stage = Stage.PLAN
    state.status = Status.AWAITING_MEDIATION
    state.artifacts["pendingMediation"] = "7_plan::PLAN_REFINEMENT"
    ctx.store.save(state)

    # Intent-only: placeholder content is not validated on the MCP wire.
    payload = json.dumps(
        {
            "summary": "test",
            "create": [
                {
                    "path": "new_feature.py",
                    "purpose": "new feature module",
                    "changeSummary": "implement feature X",
                }
            ],
            "modify": [],
        }
    )
    out = json.loads(mediation.submit_mediation(ctx, run_id, "7_plan::PLAN_REFINEMENT", payload))
    assert "trivial placeholders" not in out.get("message", "")
