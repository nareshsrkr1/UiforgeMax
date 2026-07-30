"""SERVER_FRESHNESS must catch a long-running MCP process whose in-memory
code no longer matches what's on disk (Python doesn't hot-reload modules)."""

from __future__ import annotations

import os

from uiforgemax import stale_check


def test_fresh_process_reports_no_staleness():
    assert stale_check.check_stale() is None


def test_stale_process_detected_after_source_changes(tmp_path, monkeypatch):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n", encoding="utf-8")

    monkeypatch.setattr(stale_check, "_PACKAGE_ROOT", pkg)
    monkeypatch.setattr(stale_check, "IMPORT_TIME_FINGERPRINT", stale_check._fingerprint(pkg))

    assert stale_check.check_stale() is None

    # Simulate a git pull landing on disk after the server process started —
    # force a distinct mtime so the test doesn't depend on filesystem tick resolution.
    mod = pkg / "mod.py"
    mod.write_text("x = 22\n", encoding="utf-8")
    st = mod.stat()
    os.utime(mod, ns=(st.st_atime_ns + 10_000_000, st.st_mtime_ns + 10_000_000))

    warning = stale_check.check_stale()
    assert warning is not None
    assert "STALE MCP SERVER PROCESS" in warning
    assert "Restart the MCP server connection" in warning
