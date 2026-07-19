"""Playwright optional suite — one-shot install + soft-skip when unavailable."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.pipeline.testing import (
    _is_playwright_entry,
    _run_mediated_commands,
    is_safe_install_command,
)
from uiforgemax.pipeline.toolchain import ensure_playwright, playwright_package_present


def test_playwright_install_allowlisted():
    ok, _ = is_safe_install_command("npx playwright install chromium")
    assert ok
    ok, _ = is_safe_install_command("npm install -D @playwright/test")
    assert ok


def test_is_playwright_entry_by_suite_and_command():
    assert _is_playwright_entry({"suite": "playwright", "command": "npx playwright test"}, "npx playwright test")
    assert _is_playwright_entry({"command": "npx playwright test e2e"}, "npx playwright test e2e")
    assert not _is_playwright_entry({"command": "npx vitest run"}, "npx vitest run")


def test_ensure_playwright_skips_second_attempt(tmp_path: Path):
    ws = tmp_path / "app"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    marker = tmp_path / "attempt.json"
    marker.write_text(json.dumps({"available": False}), encoding="utf-8")
    status = ensure_playwright(ws, attempt_marker=marker)
    assert status["available"] is False
    assert status.get("skipped") is True
    assert "already attempted" in status["reason"]


def test_playwright_package_present_scoped(tmp_path: Path):
    ws = tmp_path / "mono"
    pkg = ws / "node_modules" / "@playwright" / "test"
    pkg.mkdir(parents=True)
    assert playwright_package_present(ws)


def test_soft_skip_playwright_when_unavailable(tmp_path: Path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text("{}", encoding="utf-8")
    run_dir = tmp_path / "run"
    (run_dir / "tests").mkdir(parents=True)

    def fake_ensure(ws, attempt_marker=None):
        status = {"available": False, "reason": "simulated install failure", "cwd": str(ws)}
        if attempt_marker is not None:
            attempt_marker.write_text(json.dumps(status), encoding="utf-8")
        return status

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""
        timed_out = False

    monkeypatch.setattr("uiforgemax.pipeline.testing.ensure_playwright", fake_ensure)
    monkeypatch.setattr(
        "uiforgemax.pipeline.testing.run_with_timeout",
        lambda *a, **k: _Proc(),
    )
    monkeypatch.setattr("uiforgemax.pipeline.testing.rewrite_js_command", lambda c: c)

    ran, failures, _cov, pw = _run_mediated_commands(
        {"default": root},
        {
            "run": [
                {"command": "npx vitest run src/App.test.tsx", "cwd": ".", "root": "default"},
                {
                    "command": "npx playwright test",
                    "cwd": ".",
                    "root": "default",
                    "suite": "playwright",
                },
            ]
        },
        {},
        run_dir=run_dir,
    )
    soft_pw = [f for f in failures if f.startswith("[soft]") and "Playwright unavailable" in f]
    assert soft_pw
    assert ran is True  # unit command still executed
    assert pw.get("requested") is True
    assert pw.get("available") is False
    assert pw.get("status") == "unavailable"
    assert "npx playwright test" in (pw.get("skippedCommands") or [])
