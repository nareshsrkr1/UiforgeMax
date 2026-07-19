"""Implement is content-driven — no product/demo-specific patch inventing."""

from __future__ import annotations

from pathlib import Path

from uiforgemax.pipeline.implement import apply_plan


def test_apply_plan_writes_mediated_content(tmp_path: Path):
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


def test_modify_without_content_is_skipped_not_invented(tmp_path: Path):
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
    summary = apply_plan(root, plan)
    assert summary["fileCount"] == 0
    assert len(summary["skipped"]) == 1
    assert "content=" in summary["skipped"][0]["reason"]
    assert (root / "src" / "App.tsx").read_text(encoding="utf-8") == original


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
