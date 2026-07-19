"""Runtime data paths — all temp/run artifacts live under APPDATA (or system temp)."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def app_data_root() -> Path:
    """Primary storage: ``%%APPDATA%%/UiForgeMax`` on Windows.

    ``UIFORGEMAX_DATA_ROOT`` overrides this outright. This matters on Windows
    when the MCP server's Python comes from the Microsoft Store: Windows
    silently virtualizes that interpreter's AppData writes into a private
    per-package cache, so files it writes under the "real" %APPDATA% path are
    invisible to any other process (e.g. the IDE agent's own file tools)
    looking at the same nominal path. Point this at a plain, non-AppData
    folder to sidestep that entirely.
    """
    override = os.getenv("UIFORGEMAX_DATA_ROOT")
    if override:
        root = Path(override)
    else:
        appdata = os.getenv("APPDATA")
        if appdata:
            root = Path(appdata) / "UiForgeMax"
        else:
            root = Path(tempfile.gettempdir()) / "UiForgeMax"
    root.mkdir(parents=True, exist_ok=True)
    return root


def default_runs_root() -> Path:
    root = app_data_root() / "runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def project_graph_dir(project_root: str | Path) -> Path:
    """Per-repo Graphify artifacts live in the project itself: ``<root>/.uiforgemax/graph/``.

    This is the canonical place for a component's own index so later runs (and
    humans) can inspect ``ui/.uiforgemax/graph/index.json`` or
    ``backend/.uiforgemax/graph/index.json`` without digging through APPDATA.
    """
    root = Path(project_root) / ".uiforgemax" / "graph"
    root.mkdir(parents=True, exist_ok=True)
    return root


def graph_cache_root(project_root: str | Path) -> Path:
    """Secondary APPDATA mirror of a project's graph (hashed by absolute path).

    Kept for backwards compatibility / tooling that shouldn't write into the
    repo. Prefer :func:`project_graph_dir` as the source of truth.
    """
    key = hashlib.sha256(str(Path(project_root).resolve()).encode()).hexdigest()[:16]
    root = app_data_root() / "graph" / key
    root.mkdir(parents=True, exist_ok=True)
    return root
