"""Thin wrappers around the real Graphify CLI (``graphifyy`` → ``python -m graphify``)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any


class GraphifyCliError(RuntimeError):
    pass


def _is_store_python_alias(exe: str | None) -> bool:
    if not exe:
        return False
    normalized = str(exe).replace("/", "\\").lower()
    return "windowsapps" in normalized or "pythonsoftwarefoundation.python" in normalized


def resolve_python_spec(spec: str | None) -> str | None:
    """Resolve a python *path* or *command name* to an absolute executable.

    Accepts:
    - Absolute/relative path to ``python.exe`` / ``python``
    - A bare command on PATH, e.g. ``python`` or ``python3`` (via ``shutil.which``)
    """
    if not spec:
        return None
    raw = str(spec).strip().strip('"').strip("'")
    if not raw:
        return None

    path = Path(raw)
    if path.exists() and path.is_file():
        return str(path.resolve())

    # Bare command (or path that isn't a file yet) — look up on PATH.
    found = shutil.which(raw)
    if found:
        found_path = Path(found)
        if found_path.exists():
            return str(found_path.resolve())
    return None


def _python_has_graphify(exe: str, *, timeout: float = 3.0) -> bool:
    try:
        proc = subprocess.run(
            [exe, "-c", "import graphify"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _accept_graphify_python(exe: str | None) -> str | None:
    """Return absolute exe if ``import graphify`` succeeds (short timeout).

    Microsoft Store aliases are allowed only when the probe passes — they often
    hang under MCP, but some machines have Graphify on that interpreter.
    """
    if not exe or not Path(exe).exists():
        return None
    return exe if _python_has_graphify(exe, timeout=3.0) else None


@lru_cache(maxsize=1)
def graphify_python() -> str:
    """Resolve once: env override (path or command), else MCP venv / candidates."""
    override = os.environ.get("UIFORGEMAX_GRAPHIFY_PYTHON")
    if override:
        resolved = resolve_python_spec(override)
        accepted = _accept_graphify_python(resolved) if resolved else None
        if accepted:
            return accepted

    # Fast path — MCP server venv already imported graphify in-process.
    try:
        import graphify  # noqa: F401

        if not _is_store_python_alias(sys.executable):
            return sys.executable
        # Store MCP python with in-process graphify is usable.
        return sys.executable
    except ImportError:
        pass

    candidates = [sys.executable]
    try:
        pkg = Path(__file__).resolve().parents[3]  # .../UiforgeMax
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
            candidate = pkg / rel
            if candidate.exists():
                candidates.append(str(candidate))
    except Exception:  # noqa: BLE001
        pass

    for exe in candidates:
        accepted = _accept_graphify_python(exe)
        if accepted:
            return accepted
    return sys.executable


def ensure_graphify_available(exe: str | None = None) -> dict[str, Any]:
    python = exe or graphify_python()
    try:
        proc = subprocess.run(
            [python, "-c", "import graphify; print('ok')"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        # Exit 0 is enough — under MCP/OneDrive, stdout can be empty even when
        # import succeeded (print swallowed / encoding), which used to false-fail.
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        ok = proc.returncode == 0
        return {
            "ok": ok,
            "python": python,
            "detail": out or err or f"exit {proc.returncode}",
        }
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "python": python, "detail": str(exc)}


def _existing_graph_result(root: Path, *, reason: str) -> dict[str, Any] | None:
    """Return a usable graphify-out result if graph.json already has nodes."""
    out_dir = root / "graphify-out"
    graph_path = out_dir / "graph.json"
    if not graph_path.exists():
        return None
    summary = _summarize_graph(graph_path)
    if int(summary.get("nodeCount") or 0) <= 0:
        return None
    return {
        "root": str(root),
        "graphifyOut": str(out_dir),
        "graphJson": str(graph_path),
        "reused": True,
        "reuseReason": reason,
        "stdout": f"reused existing graphify-out/graph.json ({reason})",
        "stderr": "",
        **summary,
    }


_DEFAULT_GRAPHIFYIGNORE = """\
# Auto-written by UiForgeMax on first index — keeps graphify off dependency
# trees, build output, and caches for ANY stack. Edit freely; UiForgeMax
# never overwrites this file once it exists.
node_modules/
dist/
build/
out/
.next/
.nuxt/
.svelte-kit/
coverage/
.nyc_output/
.turbo/
.cache/
.parcel-cache/
.vite/
.venv/
venv/
env/
__pycache__/
*.pyc
.pytest_cache/
.mypy_cache/
.tox/
target/
bin/
obj/
vendor/
.git/
.svn/
.hg/
graphify-out/
*.log
.DS_Store
"""


def _ensure_graphifyignore(root: Path) -> None:
    """Write a generic, stack-agnostic .graphifyignore once per project.

    graphify already merges .gitignore + .graphifyignore (excludes only ever
    grow, never re-include), so this is pure insurance — it guarantees
    dependency/build/cache trees are skipped even when a repo's .gitignore is
    missing, incomplete, or intentionally tracks generated files. Never
    overwrites an existing file, so any project-specific customization
    persists across runs.
    """
    ignore_path = root / ".graphifyignore"
    if ignore_path.exists():
        return
    try:
        ignore_path.write_text(_DEFAULT_GRAPHIFYIGNORE, encoding="utf-8")
    except OSError:
        pass  # best-effort — indexing still works via .gitignore alone


def run_update(project_root: Path, *, force: bool = False, no_cluster: bool = True) -> dict[str, Any]:
    """Run ``python -m graphify update <root>`` → ``<root>/graphify-out/graph.json``.

    Reuses a non-empty existing graph unless ``force=True`` (avoids 3+ minute
    reindexes on every advance retry). Timeout defaults to 420s; override with
    ``UIFORGEMAX_GRAPHIFY_UPDATE_TIMEOUT``. On timeout, falls back to an existing
    graph when available instead of failing the whole run.
    """
    root = Path(project_root).resolve()
    out_dir = root / "graphify-out"
    graph_path = out_dir / "graph.json"
    _ensure_graphifyignore(root)

    if not force:
        reused = _existing_graph_result(root, reason="warm graphify-out present")
        if reused:
            return reused

    cmd = [graphify_python(), "-m", "graphify", "update", str(root)]
    if force:
        cmd.append("--force")
    if no_cluster:
        cmd.append("--no-cluster")

    timeout = int(os.environ.get("UIFORGEMAX_GRAPHIFY_UPDATE_TIMEOUT", "420"))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(root))
    except subprocess.TimeoutExpired as exc:
        reused = _existing_graph_result(root, reason=f"update timed out after {timeout}s; using prior graph")
        if reused:
            reused["stderr"] = str(exc)
            return reused
        raise GraphifyCliError(
            f"graphify update timed out after {timeout}s for {root} and no reusable graph.json exists. "
            f"Set UIFORGEMAX_GRAPHIFY_UPDATE_TIMEOUT higher or pre-warm graphify-out/."
        ) from exc

    if proc.returncode != 0 or not graph_path.exists():
        reused = _existing_graph_result(root, reason="update failed; using prior graph")
        if reused:
            reused["stderr"] = (proc.stderr or proc.stdout or "").strip()
            return reused
        raise GraphifyCliError(
            f"graphify update failed for {root} (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )

    summary = _summarize_graph(graph_path)
    return {
        "root": str(root),
        "graphifyOut": str(out_dir),
        "graphJson": str(graph_path),
        "reused": False,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        **summary,
    }


def run_query(
    question: str,
    graph_json: Path,
    *,
    budget: int = 1200,
    timeout: int = 25,
) -> dict[str, Any]:
    """Run ``python -m graphify query "…" --graph <graph.json>``.

    Soft-fails on timeout (returns status=timeout) so one slow query cannot
    block the whole MCP ``advance`` call.
    """
    graph_json = Path(graph_json)
    if not graph_json.exists():
        return {
            "question": question,
            "status": "missing_graph",
            "answerText": "",
            "nodes": [],
            "error": f"graph not found: {graph_json}",
        }

    # Tiny graphs: keep budget small — query is lexical BFS, not a heavy model.
    try:
        node_count = _summarize_graph(graph_json)["nodeCount"]
    except Exception:  # noqa: BLE001
        node_count = 0
    if node_count and node_count < 40:
        budget = min(budget, 800)

    cmd = [
        graphify_python(),
        "-m",
        "graphify",
        "query",
        question[:72],  # short keywords only — long NL is the main timeout driver
        "--graph",
        str(graph_json),
        "--budget",
        str(budget),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(graph_json.parent),  # graphify-out/ — avoid walking up into OneDrive root
        )
    except subprocess.TimeoutExpired:
        return {
            "question": question,
            "status": "timeout",
            "answerText": "",
            "nodes": [],
            "error": f"graphify query timed out after {timeout}s",
        }
    except OSError as exc:
        return {
            "question": question,
            "status": "error",
            "answerText": "",
            "nodes": [],
            "error": str(exc),
        }

    text = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    nodes = _parse_query_nodes(text)
    status = "ok"
    if proc.returncode != 0:
        status = "error"
    elif "No matching nodes found" in text or not nodes:
        status = "empty"

    return {
        "question": question,
        "status": status,
        "answerText": text,
        "nodes": nodes,
        "stderr": err,
        "exitCode": proc.returncode,
    }


def run_merge_graphs(graph_jsons: list[Path], out_path: Path) -> dict[str, Any]:
    """Run ``python -m graphify merge-graphs … --out <path>`` when multiple roots exist."""
    if len(graph_jsons) < 2:
        raise GraphifyCliError("merge-graphs needs at least two graph.json files")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [graphify_python(), "-m", "graphify", "merge-graphs", *[str(p) for p in graph_jsons], "--out", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out_path.exists():
        raise GraphifyCliError(
            f"graphify merge-graphs failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    summary = _summarize_graph(out_path)
    return {"mergedGraph": str(out_path), **summary, "stdout": (proc.stdout or "").strip()}


_graph_summary_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _summarize_graph(graph_path: Path) -> dict[str, Any]:
    key = str(graph_path)
    try:
        mtime = graph_path.stat().st_mtime
    except OSError:
        return {"nodeCount": 0, "edgeCount": 0, "sourceFiles": []}

    cached = _graph_summary_cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"nodeCount": 0, "edgeCount": 0, "sourceFiles": []}
    nodes = data.get("nodes") or []
    links = data.get("links") or data.get("edges") or []
    files = sorted(
        {
            n.get("source_file")
            for n in nodes
            if isinstance(n, dict) and n.get("source_file")
        }
    )
    result = {"nodeCount": len(nodes), "edgeCount": len(links), "sourceFiles": files}
    _graph_summary_cache[key] = (mtime, result)
    return result


_NODE_LINE = re.compile(
    r"^NODE\s+(?P<label>.+?)\s+\[src=(?P<source>[^\s]*)\s+loc=(?P<loc>[^\s]*)",
    re.M,
)


def _parse_query_nodes(text: str) -> list[dict[str, str]]:
    nodes: list[dict[str, str]] = []
    for match in _NODE_LINE.finditer(text or ""):
        nodes.append(
            {
                "label": match.group("label").strip(),
                "source_file": match.group("source").strip(),
                "source_location": match.group("loc").strip(),
            }
        )
    return nodes
