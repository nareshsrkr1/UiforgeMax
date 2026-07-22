"""Auto-written .graphifyignore: generic, stack-agnostic, never overwritten."""

from __future__ import annotations

from pathlib import Path

from uiforgemax.graphify.cli_runner import _ensure_graphifyignore


def test_writes_default_when_missing(tmp_path: Path):
    _ensure_graphifyignore(tmp_path)
    ignore_path = tmp_path / ".graphifyignore"
    assert ignore_path.exists()
    content = ignore_path.read_text(encoding="utf-8")
    assert "node_modules/" in content
    assert "__pycache__/" in content
    assert "target/" in content
    assert "vendor/" in content


def test_never_overwrites_existing_file(tmp_path: Path):
    ignore_path = tmp_path / ".graphifyignore"
    ignore_path.write_text("custom-only-line/\n", encoding="utf-8")
    _ensure_graphifyignore(tmp_path)
    assert ignore_path.read_text(encoding="utf-8") == "custom-only-line/\n"


def test_idempotent_across_calls(tmp_path: Path):
    _ensure_graphifyignore(tmp_path)
    first = (tmp_path / ".graphifyignore").read_text(encoding="utf-8")
    _ensure_graphifyignore(tmp_path)
    second = (tmp_path / ".graphifyignore").read_text(encoding="utf-8")
    assert first == second
