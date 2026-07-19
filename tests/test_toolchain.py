"""Toolchain resolve + auto-install helpers."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from uiforgemax.pipeline.testing import is_safe_install_command
from uiforgemax.pipeline.toolchain import (
    find_js_workspace_root,
    js_deps_ready,
    rewrite_js_command,
    run_with_timeout,
)


def test_find_js_workspace_walks_up(tmp_path: Path):
    mono = tmp_path / "platform"
    app = mono / "apps" / "portal"
    app.mkdir(parents=True)
    (mono / "package.json").write_text("{}", encoding="utf-8")
    (mono / "package-lock.json").write_text("{}", encoding="utf-8")
    (app / "package.json").write_text("{}", encoding="utf-8")
    # Prefer monorepo root that has the lockfile (Nx)
    assert find_js_workspace_root(app) == mono.resolve()


def test_find_js_workspace_prefers_workspaces_root(tmp_path: Path):
    mono = tmp_path / "platform"
    app = mono / "apps" / "portal"
    app.mkdir(parents=True)
    (mono / "package.json").write_text('{"workspaces":["apps/*"]}', encoding="utf-8")
    (app / "package.json").write_text("{}", encoding="utf-8")
    assert find_js_workspace_root(app) == mono.resolve()


def test_find_js_workspace_nearest_without_lock(tmp_path: Path):
    mono = tmp_path / "platform"
    app = mono / "apps" / "portal"
    app.mkdir(parents=True)
    (mono / "package.json").write_text("{}", encoding="utf-8")
    (app / "package.json").write_text("{}", encoding="utf-8")
    assert find_js_workspace_root(app) == app.resolve()


def test_js_deps_ready(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text("{}", encoding="utf-8")
    assert not js_deps_ready(ws)
    nm = ws / "node_modules"
    nm.mkdir()
    (nm / "vitest").mkdir()
    assert js_deps_ready(ws)


def test_npm_cmd_ci_allowlisted():
    ok, _ = is_safe_install_command("npm.cmd ci")
    assert ok
    ok, _ = is_safe_install_command(r'"C:\Program Files\nodejs\npm.cmd" ci')
    assert ok


def test_rewrite_js_command_with_fake_npm(monkeypatch, tmp_path: Path):
    # Env is last-resort after PATH / Program Files — stub discovery so override applies.
    npm = tmp_path / "npm.cmd"
    npm.write_text("", encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_text("", encoding="utf-8")
    monkeypatch.setenv("UIFORGEMAX_NPM", str(npm))
    monkeypatch.setenv("UIFORGEMAX_NODE", str(node))
    monkeypatch.setattr(
        "uiforgemax.pipeline.toolchain.which_tool",
        lambda *names: None,
    )
    monkeypatch.setattr(
        "uiforgemax.pipeline.toolchain._windows_node_candidates",
        lambda: [],
    )
    out = rewrite_js_command("npx vitest run")
    assert "vitest" in out
    assert str(npm) in out or "exec" in out


def test_run_with_timeout_kills_grandchild_not_just_shell(tmp_path: Path):
    """Regression: shell=True's immediate child (cmd.exe/sh) surviving timeout
    isn't enough — a surviving grandchild (npm.cmd -> node.exe) keeps stdio
    pipes open and hangs communicate() forever. run_with_timeout must kill
    the whole tree so callers reliably get control back near ``timeout``,
    not after the grandchild's own long-running work finishes.
    """
    script = tmp_path / "spawn_grandchild.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "time.sleep(20)\n",
        encoding="utf-8",
    )
    cmd = f'"{sys.executable}" "{script}"'
    start = time.monotonic()
    result = run_with_timeout(cmd, cwd=tmp_path, timeout=2)
    elapsed = time.monotonic() - start

    assert result.timed_out is True
    # Well under the grandchild's 20s sleep — proves the tree was killed
    # rather than communicate() blocking on the surviving grandchild's pipes.
    assert elapsed < 12
