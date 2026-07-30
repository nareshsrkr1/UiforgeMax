"""Detect a stale, long-running MCP server process.

The MCP server is a single long-lived OS process (started once when the IDE
connects) that can stay alive for days across many runs. Python does not
reload a module after it's been imported, so if the source on disk changes
(git pull/checkout/edit) after the process started, every stage that process
runs keeps executing the OLD code in memory — including safety gates like
VISUAL_VALIDATE — even though `git log` / the files on disk already show the
fix. `uiforgemax_start_run` only creates a fresh *run*; it cannot make an
already-running process re-read its own modules.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(str(stat.st_mtime_ns).encode("utf-8"))
        digest.update(str(stat.st_size).encode("utf-8"))
    return digest.hexdigest()


# Captured once, at import time — i.e. the moment this MCP server process started.
IMPORT_TIME_FINGERPRINT = _fingerprint(_PACKAGE_ROOT)


def check_stale() -> str | None:
    """Return a warning message if on-disk source has changed since this
    process started, else None. Cheap enough to call every preflight."""
    current = _fingerprint(_PACKAGE_ROOT)
    if current == IMPORT_TIME_FINGERPRINT:
        return None
    return (
        "STALE MCP SERVER PROCESS: the uiforgemax source on disk has changed "
        "since this server process started, but Python does not reload "
        "already-imported modules. Every stage this process runs — including "
        "safety gates like VISUAL_VALIDATE — is still executing the OLD code "
        "in memory, regardless of what git/the files show. Restart the MCP "
        "server connection (reload window, or disable + re-enable the "
        "uiforgemax MCP server) before starting or resuming any run."
    )
