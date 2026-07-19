"""Session-scoped environment verified by preflight (persists across agent turns)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uiforgemax.paths import app_data_root


def session_path() -> Path:
    return app_data_root() / "session.json"


def load_session() -> dict[str, Any]:
    path = session_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_session(data: dict[str, Any]) -> Path:
    path = session_path()
    data["updatedAt"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def workspace_root(session: dict[str, Any] | None = None) -> str | None:
    s = session if session is not None else load_session()
    root = s.get("workspaceRoot")
    return str(root) if root else None


def python_executable(session: dict[str, Any] | None = None) -> str | None:
    s = session if session is not None else load_session()
    exe = s.get("pythonExecutable")
    return str(exe) if exe else None
