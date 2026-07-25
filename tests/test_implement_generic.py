"""Implement is content-driven — no product/demo-specific patch inventing."""

from __future__ import annotations

from pathlib import Path

import pytest

from uiforgemax.pipeline.implement import apply_plan, PartialImplementError


def test_apply_plan_writes_mediated_content(tmp_path: Path):
    """When every action has content= supplied, the plan succeeds cleanly."""
    root = tmp_path / "any-app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text("export function App() { return null }\n", encoding="utf-8")

    plan = {
        "create": [
            {
                "path": "src/feature/NewThing.tsx",
                "purpose": "new feature",
                "content": "export function NewThing() { return <div>ok</div> }\n",
                "root": "default",
            }
        ],
        "modify": [
            {
                "path": "src/App.tsx",
                "purpose": "wire feature",
                "content": (
                    "import { NewThing } from './feature/NewThing'\n"
                    "export function App() { return <NewThing /> }\n"
                ),
                "root": "default",
            }
        ],
        "executionOrder": ["src/feature/NewThing.tsx", "src/App.tsx"],
    }
    summary = apply_plan(root, plan)
    assert summary["fileCount"] == 2
    assert summary["skipped"] == []
    assert "NewThing" in (root / "src" / "feature" / "NewThing.tsx").read_text(encoding="utf-8")
    assert "NewThing" in (root / "src" / "App.tsx").read_text(encoding="utf-8")


def test_all_skipped_plan_raises_not_silent_success(tmp_path: Path):
    """All-skipped (0 written) must hard-fail — never advance as a successful implement."""
    root = tmp_path / "any-app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text("export function App() { return null }\n", encoding="utf-8")
    original = (root / "src" / "App.tsx").read_text(encoding="utf-8")

    plan = {
        "modify": [
            {
                "path": "src/App.tsx",
                "purpose": "add dark theme somehow",
                "patchId": "shell-dark-class",  # no longer a built-in product patch
                "root": "default",
            }
        ],
        "executionOrder": ["src/App.tsx"],
    }
    with pytest.raises(PartialImplementError) as exc:
        apply_plan(root, plan)
    assert exc.value.changed == []
    assert len(exc.value.skipped) == 1
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == original


def test_partial_skip_raises_error(tmp_path: Path):
    """When some files write but others skip, PartialImplementError is raised.

    This prevents silently committing broken/incomplete code.
    """
    root = tmp_path / "any-app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text("export function App() { return null }\n", encoding="utf-8")

    plan = {
        "create": [
            # This will succeed — content is supplied
            {"path": "NewFile.tsx", "content": "export const X = 1;\n", "purpose": "new", "root": "default"}
        ],
        "modify": [
            # This will fail — no content, unknown patchId
            {"path": "src/App.tsx", "purpose": "unknown patch", "patchId": "nonexistent", "root": "default"}
        ],
        "executionOrder": ["NewFile.tsx", "src/App.tsx"],
    }
    with pytest.raises(PartialImplementError) as exc_info:
        apply_plan(root, plan)

    err = exc_info.value
    assert "NewFile.tsx" in err.changed
    assert len(err.skipped) == 1
    assert err.skipped[0]["path"] == "src/App.tsx"
    assert "PARTIAL IMPLEMENT" in str(err)
    assert "PLAN_REFINEMENT" in str(err)


def test_greenfield_template_still_works(tmp_path: Path):
    root = tmp_path / "empty"
    root.mkdir()
    plan = {
        "create": [
            {
                "path": "README.md",
                "purpose": "overview",
                "templateId": "greenfield.readme",
            }
        ],
        "executionOrder": ["README.md"],
    }
    summary = apply_plan(root, plan)
    assert summary["fileCount"] == 1
    assert "UiForgeMax" in (root / "README.md").read_text(encoding="utf-8")

