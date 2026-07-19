"""Cheap filesystem stack detection — before real Graphify runs.

Nx is a *flavor* flag, never a hard gate. Detection is intentional and local
(nx.json / FastAPI imports / React markers), not inferred only from a thin graph.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_IGNORED = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "graphify-out", ".uiforgemax"}


def detect_stack(project_root: Path) -> dict[str, Any]:
    root = Path(project_root)
    empty = _is_empty(root)
    # Nx config often lives on the monorepo parent while project_root is apps/<name>.
    nx = _has_nx_marker(root)
    fastapi = _has_text_marker(root, (".py",), ("FastAPI", "from fastapi", "import fastapi"))
    react = (
        any(root.rglob("*.tsx"))
        or any(root.rglob("*.jsx"))
        or _has_text_marker(root, (".html", ".tsx", ".jsx", ".ts", ".js"), ("react", "ReactDOM", 'from "react"'))
    )
    express = _has_text_marker(root, (".ts", ".js"), ("express()", 'from "express"', "from 'express'"))

    kinds: list[str] = []
    if nx:
        kinds.append("nx")
    if fastapi:
        kinds.append("fastapi")
    if express:
        kinds.append("express")
    if react:
        kinds.append("react")

    if empty:
        primary = "greenfield"
    elif nx:
        primary = "nx"
    elif fastapi and react:
        primary = "fastapi-react"
    elif fastapi:
        primary = "fastapi"
    elif react:
        primary = "react"
    elif express:
        primary = "express"
    else:
        primary = "standard"

    return {
        "primary": primary,
        "kinds": kinds,
        "nx": nx,
        "fastapi": fastapi,
        "react": react,
        "express": express,
        "empty": empty,
        "path": str(root.resolve()),
    }


def _has_nx_marker(root: Path, *, max_up: int = 4) -> bool:
    cur = root.resolve()
    for _ in range(max_up + 1):
        if (cur / "nx.json").exists() or (cur / "workspace.json").exists():
            return True
        if cur.parent == cur:
            break
        cur = cur.parent
    return False


def _is_empty(root: Path) -> bool:
    try:
        return not any(root.iterdir())
    except OSError:
        return True


def _has_text_marker(root: Path, suffixes: tuple[str, ...], markers: tuple[str, ...], limit_files: int = 80) -> bool:
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _IGNORED for part in path.parts):
            continue
        if path.suffix.lower() not in suffixes:
            continue
        checked += 1
        if checked > limit_files:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(m in text for m in markers):
            return True
    return False
