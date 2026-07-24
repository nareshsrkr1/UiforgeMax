"""Thin wrappers around the real Graphify CLI (``graphifyy`` → ``python -m graphify``)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.toolchain import _kill_process_tree


class GraphifyCliError(RuntimeError):
    pass


def _clean_subprocess_env() -> dict[str, str]:
    """Environment for spawned graphify processes — without UiForgeMax's own paths.

    The MCP server is launched (via mcp.json) with ``PYTHONPATH`` pointing at
    UiForgeMax's ``src`` so it can import ``uiforgemax``. A plain
    ``subprocess.Popen`` inherits that, so every ``python -m graphify`` call —
    AND each of graphify's ~16 AST worker subprocesses — starts up with that
    extra sys.path root. graphify is installed in site-packages and does not
    need it; inheriting it only adds per-process import-resolution overhead,
    which is badly amplified on machines where endpoint AV scans every file
    open (manual runs from a clean shell don't carry this and stay fast).
    Strip ``PYTHONPATH`` (and UiForgeMax's own env vars) so the subprocess env
    matches a clean manual invocation.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    for key in list(env):
        if key.startswith("UIFORGEMAX_"):
            env.pop(key, None)
    # Force unbuffered stdout/stderr so graphify's progress lines appear in the
    # live log immediately, even if the process later hangs. Without this,
    # piped stdout is block-buffered and a hang shows an empty log (output stuck
    # in an unflushed buffer) — exactly the "started, then silence" symptom.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _run_logged(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, bool]:
    """Run ``cmd``, streaming output to ``log_path`` LIVE (tail-able from a second
    terminal while it runs), with the exact command/cwd/timeout recorded up front.

    Unlike ``subprocess.run(..., capture_output=True)``, which buffers
    everything silently until the process ends or is killed, this writes each
    output line to disk as it arrives — so a hang or a slow scan is visible in
    real time (``Get-Content -Wait <log_path>``), not just a static timeout
    message after the fact with zero insight into what was actually running.

    Returns ``(returncode, combined_output, timed_out)``. On timeout the whole
    process tree is killed (not just the top-level process) so a grandchild
    spawned by a git/graphify hook cannot keep running or hold file handles.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    inherited_pythonpath = os.environ.get("PYTHONPATH", "(unset)")
    subproc_pythonpath = (env or {}).get("PYTHONPATH", "(unset)")
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(
            f"\n=== {now} ===\n"
            f"cmd: {' '.join(cmd)}\n"
            f"cwd: {cwd}\n"
            f"timeout: {timeout}s\n"
            f"MCP PYTHONPATH (inherited): {inherited_pythonpath}\n"
            f"subprocess PYTHONPATH (used): {subproc_pythonpath}\n"
            "--- output (live) ---\n"
        )
        logf.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,  # never inherit the MCP server's JSON-RPC
            # stdin — if graphify/a dep reads stdin (update prompt, confirmation)
            # it must get instant EOF, not hang forever waiting on a pipe that
            # never delivers input. This is the classic "works in a terminal,
            # hangs when spawned by a server" bug.
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        lines: list[str] = []

        def _drain() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.append(line)
                    logf.write(line)
                    logf.flush()
            except (ValueError, OSError):
                pass  # pipe closed by the timeout-kill path below

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        reader.join(timeout)

        if reader.is_alive():
            logf.write(f"\n!!! TIMED OUT after {timeout}s — killing process tree !!!\n")
            logf.flush()
            _kill_process_tree(proc.pid)
            reader.join(5)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return None, "".join(lines), True

        proc.wait()
        logf.write(f"\n=== exit code {proc.returncode} ===\n")
        logf.flush()
        return proc.returncode, "".join(lines), False


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
            stdin=subprocess.DEVNULL,  # never block on inherited MCP stdin
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
            stdin=subprocess.DEVNULL,  # never block on inherited MCP stdin
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


def run_update(
    project_root: Path,
    *,
    force: bool = False,
    no_cluster: bool = True,
    log_dir: Path | None = None,
) -> dict[str, Any]:
    """Run ``python -m graphify update <root>`` → ``<root>/graphify-out/graph.json``.

    Reuses a non-empty existing graph unless ``force=True`` (avoids 3+ minute
    reindexes on every advance retry). Timeout defaults to 420s; override with
    ``UIFORGEMAX_GRAPHIFY_UPDATE_TIMEOUT``. On timeout, falls back to an existing
    graph when available instead of failing the whole run.

    ``log_dir``, when given, is where the live diagnostic log is written
    instead of ``<project>/graphify-out/`` — pass the UiForgeMax run directory
    so the log lives with the run's own artifacts, not inside the target
    project (which may be committed and shouldn't gain debug files).
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
    log_path = Path(log_dir) / "graphify-update.log" if log_dir else out_dir / "uiforgemax-update.log"
    returncode, output, timed_out = _run_logged(
        cmd, cwd=root, timeout=timeout, log_path=log_path, env=_clean_subprocess_env()
    )

    if timed_out:
        reused = _existing_graph_result(root, reason=f"update timed out after {timeout}s; using prior graph")
        if reused:
            reused["stderr"] = output
            reused["logFile"] = str(log_path)
            return reused
        raise GraphifyCliError(
            f"graphify update timed out after {timeout}s for {root} and no reusable graph.json exists. "
            f"Set UIFORGEMAX_GRAPHIFY_UPDATE_TIMEOUT higher or pre-warm graphify-out/. "
            f"Full live output (what it was actually doing when killed) is in: {log_path}"
        )

    if returncode != 0 or not graph_path.exists():
        reused = _existing_graph_result(root, reason="update failed; using prior graph")
        if reused:
            reused["stderr"] = output.strip()
            reused["logFile"] = str(log_path)
            return reused
        raise GraphifyCliError(
            f"graphify update failed for {root} (exit {returncode}): {output.strip()} "
            f"Full output also in: {log_path}"
        )

    summary = _summarize_graph(graph_path)
    return {
        "root": str(root),
        "graphifyOut": str(out_dir),
        "graphJson": str(graph_path),
        "logFile": str(log_path),
        "reused": False,
        "stdout": output.strip(),
        "stderr": "",
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
            env=_clean_subprocess_env(),  # don't inherit UiForgeMax PYTHONPATH into graphify
            stdin=subprocess.DEVNULL,  # never block reading the MCP server's stdin
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
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, env=_clean_subprocess_env(), stdin=subprocess.DEVNULL
    )
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
