"""IDE apply mode — verify planned paths after agent edits."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.pipeline.ide_apply import (
    build_pre_apply_baseline,
    ide_apply_brief,
    verify_ide_apply,
)
from uiforgemax.pipeline.planning import plan_is_implementable


def test_intent_only_plan_is_approvable():
    plan = {
        "create": [{"path": "a.ts", "purpose": "new"}],
        "modify": [{"path": "b.ts", "purpose": "tweak", "changeSummary": "rename"}],
    }
    assert plan_is_implementable(plan) is True


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
