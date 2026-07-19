"""MCP itself (not the driving agent) snapshots literal file content for PLAN_REFINEMENT."""

from __future__ import annotations

from pathlib import Path

from uiforgemax.graphify.source_snapshot import build_source_snapshots


def test_snapshot_reads_existing_modify_file(tmp_path: Path):
    root = tmp_path / "app"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.tsx").write_text("export function App() { return null; }", encoding="utf-8")

    plan = {"modify": [{"path": "src/App.tsx", "purpose": "theme"}], "reuse": [], "create": []}
    snap = build_source_snapshots({"default": root}, plan)

    key = "default:src/App.tsx"
    assert key in snap["files"]
    entry = snap["files"][key]
    assert entry["exists"] is True
    assert "export function App" in entry["content"]
    assert entry["truncated"] is False


def test_snapshot_missing_file_marked_not_exists(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    plan = {"modify": [{"path": "src/Missing.tsx"}]}
    snap = build_source_snapshots({"default": root}, plan)
    entry = snap["files"]["default:src/Missing.tsx"]
    assert entry["exists"] is False


def test_snapshot_binary_file_flagged_not_read(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
    plan = {"reuse": [{"path": "logo.png"}]}
    snap = build_source_snapshots({"default": root}, plan)
    entry = snap["files"]["default:logo.png"]
    assert entry["exists"] is True
    assert entry.get("binary") is True
    assert "content" not in entry


def test_snapshot_dedupes_across_buckets_prefers_modify(tmp_path: Path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "styles.css").write_text("body { color: red; }", encoding="utf-8")
    plan = {
        "modify": [{"path": "styles.css", "purpose": "dark theme"}],
        "reuse": [{"path": "styles.css"}],
    }
    snap = build_source_snapshots({"default": root}, plan)
    assert snap["fileCount"] == 1
    assert snap["files"]["default:styles.css"]["bucket"] == "modify"


def test_snapshot_truncates_large_files(tmp_path: Path, monkeypatch):
    import uiforgemax.graphify.source_snapshot as mod

    monkeypatch.setattr(mod, "MAX_FILE_BYTES", 10)
    root = tmp_path / "app"
    root.mkdir()
    (root / "big.css").write_text("a" * 100, encoding="utf-8")
    snap = mod.build_source_snapshots({"default": root}, {"modify": [{"path": "big.css"}]})
    entry = snap["files"]["default:big.css"]
    assert entry["truncated"] is True
    assert len(entry["content"]) == 10


def test_snapshot_multi_root(tmp_path: Path):
    ui = tmp_path / "ui"
    api = tmp_path / "api"
    ui.mkdir()
    api.mkdir()
    (ui / "App.tsx").write_text("x", encoding="utf-8")
    (api / "main.py").write_text("y", encoding="utf-8")
    plan = {
        "modify": [
            {"path": "App.tsx", "root": "ui"},
            {"path": "main.py", "root": "api"},
        ]
    }
    snap = build_source_snapshots({"default": ui, "ui": ui, "api": api}, plan)
    assert snap["files"]["ui:App.tsx"]["content"] == "x"
    assert snap["files"]["api:main.py"]["content"] == "y"


def test_snapshot_unknown_root_falls_back_to_default(tmp_path: Path):
    """No 'ghost' root registered — falls back to default root, file not found there."""
    plan = {"modify": [{"path": "x.ts", "root": "ghost"}]}
    snap = build_source_snapshots({"default": tmp_path}, plan)
    entry = snap["files"]["ghost:x.ts"]
    assert entry["exists"] is False
