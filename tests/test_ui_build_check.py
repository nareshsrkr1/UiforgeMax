"""UI production build check (npm run build / ng / vite) in TEST stage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from uiforgemax.pipeline.testing import (
    _mediated_already_has_build,
    resolve_ui_build_command,
    run_tests,
    run_ui_build_checks,
)


def _react_app(root: Path, *, build_script: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    scripts = {"build": "vite build", "test": "vitest run"} if build_script else {"test": "vitest"}
    (root / "package.json").write_text(
        json.dumps({"name": "ui", "scripts": scripts}),
        encoding="utf-8",
    )
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "App.tsx").write_text("export default function App(){return null}\n", encoding="utf-8")


def test_resolve_ui_build_prefers_npm_run_build(tmp_path: Path):
    app = tmp_path / "ui"
    _react_app(app)
    spec = resolve_ui_build_command(app)
    assert spec is not None
    assert spec["command"] == "npm run build"
    assert "scripts.build" in spec["reason"]


def test_resolve_skips_api_only_package(tmp_path: Path):
    api = tmp_path / "api"
    api.mkdir()
    (api / "package.json").write_text(
        json.dumps({"name": "api", "scripts": {"build": "tsc"}}),
        encoding="utf-8",
    )
    (api / "src").mkdir()
    (api / "src" / "server.js").write_text("console.log('api')\n", encoding="utf-8")
    assert resolve_ui_build_command(api) is None


def test_resolve_angular_without_scripts_build(tmp_path: Path):
    app = tmp_path / "ng"
    app.mkdir()
    (app / "angular.json").write_text("{}", encoding="utf-8")
    (app / "package.json").write_text(json.dumps({"name": "ng", "scripts": {}}), encoding="utf-8")
    (app / "src").mkdir()
    (app / "src" / "app.component.ts").write_text(
        "import { Component } from '@angular/core';\n",
        encoding="utf-8",
    )
    spec = resolve_ui_build_command(app)
    assert spec is not None
    assert spec["command"] == "npx ng build"


def test_mediated_already_has_build():
    assert _mediated_already_has_build(
        {"run": [{"command": "npm run build", "suite": "build"}]}
    )
    assert _mediated_already_has_build({"run": [{"command": "npx vite build"}]})
    assert not _mediated_already_has_build({"run": [{"command": "npx vitest run"}]})


def test_run_ui_build_checks_invokes_and_fails(tmp_path: Path, monkeypatch):
    app = tmp_path / "ui"
    _react_app(app)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    calls: list[str] = []

    def fake_run(cmd, *, cwd=None, env=None, timeout=None):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="Build failed: TS2322", timed_out=False)

    monkeypatch.setattr("uiforgemax.pipeline.testing.run_with_timeout", fake_run)
    monkeypatch.setattr("uiforgemax.pipeline.testing.rewrite_js_command", lambda c: c)

    status = run_ui_build_checks({"default": app}, {"tests": [{"path": "x"}]}, run_dir=run_dir)
    assert status["requested"] is True
    assert status["passed"] is False
    assert calls and "npm run build" in calls[0]
    assert (run_dir / "tests" / "ui-build-status.json").exists()
    assert any("[build]" in f for f in status["failures"])


def test_run_ui_build_skips_when_mediation_has_build(tmp_path: Path, monkeypatch):
    app = tmp_path / "ui"
    _react_app(app)
    monkeypatch.setattr(
        "uiforgemax.pipeline.testing.run_with_timeout",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not build")),
    )
    status = run_ui_build_checks(
        {"default": app},
        {"run": [{"command": "npm run build", "suite": "build"}]},
    )
    assert status["skipped"] is True
    assert status["requested"] is False


def test_run_tests_fail_fast_on_ui_build(tmp_path: Path, monkeypatch):
    app = tmp_path / "ui"
    _react_app(app)
    run_dir = tmp_path / "run"
    (run_dir / "tests").mkdir(parents=True)
    (run_dir / "tests" / "generated-tests.json").write_text(
        json.dumps(
            {
                "stack": {"language": "typescript", "testFramework": "vitest", "buildTool": "npm"},
                "tests": [
                    {
                        "path": "src/App.test.tsx",
                        "content": "test('x', () => {})",
                        "type": "unit",
                    }
                ],
                "run": [{"command": "npx vitest run", "cwd": ".", "root": "default", "suite": "unit"}],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "uiforgemax.pipeline.testing.run_allowlisted_installs",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "uiforgemax.pipeline.testing.ensure_dependencies_for_tests",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "uiforgemax.pipeline.testing.write_toolchain_facts",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "uiforgemax.pipeline.testing.run_with_timeout",
        lambda *a, **k: SimpleNamespace(
            returncode=2, stdout="", stderr="error TS", timed_out=False
        ),
    )
    monkeypatch.setattr("uiforgemax.pipeline.testing.rewrite_js_command", lambda c: c)

    # If unit tests ran, this would be called — ensure fail-fast skips them.
    def boom(*_a, **_k):
        raise AssertionError("unit tests should not run after build failure")

    monkeypatch.setattr("uiforgemax.pipeline.testing._run_mediated_commands", boom)

    results = run_tests(app, plan={"create": [], "modify": []}, run_dir=run_dir)
    assert results["passed"] is False
    assert results["uiBuild"].get("passed") is False
    assert any("[build]" in f for f in results["failures"])
