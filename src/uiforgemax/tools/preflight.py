"""Environment preflight — Python, Graphify (built-in), workspace, Jira."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from uiforgemax.session import load_session, save_session, session_path
from uiforgemax.tools.context import ToolContext


def preflight(
    ctx: ToolContext,
    project_root: str | None = None,
    python_executable: str | None = None,
    components: str | None = None,
    workspace_folders: str | None = None,
) -> str:
    """Verify MCP runtime, Graphify module, optional workspace(s). Persists session.json.

    ``project_root`` is the primary/default workspace. ``components`` is an optional JSON
    object of ``{"name": "path"}`` for additional components in the same run (e.g. a
    separate ``ui`` and ``backend`` repo) — each gets its own Graphify artifacts later.
    ``workspace_folders`` is an optional JSON array of the folder paths the IDE currently
    has open; when given, every path (default + each component) must resolve to one of
    those folders or a subfolder of one, otherwise that check BLOCKS.
    """
    session = load_session()
    exe = _resolve_python_executable(python_executable or session.get("pythonExecutable"))
    checks: list[dict[str, Any]] = []

    checks.append(_check_python_executable(exe))
    checks.append(_check_graphify_in_process())
    # Only probe a *different* interpreter when it isn't the flaky Store alias.
    # GRAPHIFY_MODULE already proved the MCP process can import graphify.
    if exe != sys.executable and not _is_store_python_alias(exe):
        checks.append(_check_graphify_subprocess(exe))

    folders, folders_error = _parse_json_array(workspace_folders)
    if workspace_folders and folders_error:
        checks.append({"name": "WORKSPACE_FOLDERS_ARG", "ok": False, "detail": folders_error})
    elif not workspace_folders:
        folders = session.get("ideWorkspaceFolders", [])

    components_map, components_error = _parse_json_object(components)
    if components and components_error:
        checks.append({"name": "COMPONENTS_ARG", "ok": False, "detail": components_error})

    workspace_source = "none"
    if project_root:
        checks.append(_check_workspace(project_root, folders))
        workspace_source = "explicit"
    elif session.get("workspaceRoot"):
        checks.append(_check_workspace(session["workspaceRoot"], folders))
        workspace_source = "reused_from_previous_session"

    for name, path in components_map.items():
        checks.append(_check_workspace(path, folders, check_name=f"COMPONENT:{name}"))

    jira = ctx.config.jira
    checks.append(
        {
            "name": "JIRA_CONFIG",
            "ok": jira.is_configured,
            "detail": jira.base_url or "(unset — Jira intake will BLOCK)",
        }
    )
    checks.append(
        {
            "name": "MCP_SERVER",
            "ok": True,
            "detail": f"uiforgemax MCP responding (pid {os.getpid()})",
        }
    )

    critical = {"PYTHON_EXECUTABLE", "GRAPHIFY_MODULE"}
    if project_root or session.get("workspaceRoot"):
        critical.add("WORKSPACE")
    # Subprocess check is advisory when in-process Graphify already passed —
    # Store-Python timeouts must not block a good MCP venv.
    subprocess_check = next((c for c in checks if c["name"] == "GRAPHIFY_SUBPROCESS"), None)
    if (
        subprocess_check
        and not subprocess_check["ok"]
        and any(c["name"] == "GRAPHIFY_MODULE" and c["ok"] for c in checks)
    ):
        subprocess_check["ok"] = True
        subprocess_check["detail"] = (
            f"WARNING (ignored): {subprocess_check['detail']} — "
            "in-process Graphify on MCP python is OK; not blocking preflight."
        )
    elif subprocess_check is not None:
        critical.add("GRAPHIFY_SUBPROCESS")
    if workspace_folders and folders_error:
        critical.add("WORKSPACE_FOLDERS_ARG")
    if components and components_error:
        critical.add("COMPONENTS_ARG")
    critical.update(f"COMPONENT:{name}" for name in components_map)

    all_ok = all(c["ok"] for c in checks if c["name"] in critical)
    workspace = project_root or session.get("workspaceRoot")
    workspace_check = next((c for c in checks if c["name"] == "WORKSPACE"), None)
    visible = workspace_check.get("visible", []) if workspace_check else []
    component_results = {
        name: {
            "path": next((c["detail"] for c in checks if c["name"] == f"COMPONENT:{name}"), None),
            "visible": next((c.get("visible", []) for c in checks if c["name"] == f"COMPONENT:{name}"), []),
        }
        for name in components_map
    }

    if all_ok:
        session.update(
            {
                "pythonExecutable": exe,
                "graphifyReady": True,
                "graphifyModule": "graphify",
                "mcpPythonExecutable": sys.executable,
            }
        )
        if project_root:
            session["workspaceRoot"] = str(Path(project_root).resolve())
        if workspace_folders:
            session["ideWorkspaceFolders"] = folders
        if components_map:
            session["projectRoots"] = {
                **session.get("projectRoots", {}),
                **{name: str(Path(p).resolve()) for name, p in components_map.items()},
            }
        save_session(session)

    from uiforgemax.session import session_path as _session_path

    body: dict[str, Any] = {
        "ok": all_ok,
        "stop": not all_ok,
        "message": "Preflight passed — session saved." if all_ok else "Preflight FAILED — fix checks before starting a run.",
        "checks": checks,
        "session": session if all_ok else load_session(),
        "sessionPath": str(_session_path()),
        "workspace": workspace,
        "workspaceSource": workspace_source,
        "workspaceContents": visible,
        "components": component_results,
        "nextTool": "uiforgemax_start_run" if all_ok else "uiforgemax_preflight",
        "alternatives": ["uiforgemax_get_pipeline_guide"],
        "blockedTools": [] if all_ok else ["uiforgemax_start_run", "uiforgemax_advance"],
        "note": (
            "Uses the real Graphify CLI (`pip install graphifyy` → `python -m graphify`). "
            "Each component root gets `<repo>/graphify-out/` from `graphify update`. "
            "The pythonExecutable saved here is used for the entire agent session."
        ),
    }
    if workspace_source == "reused_from_previous_session":
        body["warning"] = (
            f"No project_root passed — reused workspace from a PREVIOUS session: {workspace}. "
            "If you meant a different folder for this chat, call preflight again with the "
            "correct project_root before starting a run."
        )
    if not all_ok and not python_executable and checks[0]["name"] == "PYTHON_EXECUTABLE" and not checks[0]["ok"]:
        body["hint"] = "Pass python_executable='C:/path/to/.venv/Scripts/python.exe' to preflight if MCP uses a different venv."

    return json.dumps(body, indent=2)


def set_workspace(ctx: ToolContext, project_root: str, run_id: str | None = None) -> str:
    """Set workspace for session and optionally attach to an existing run."""
    session = load_session()
    check = _check_workspace(project_root, session.get("ideWorkspaceFolders", []))
    if not check["ok"]:
        return json.dumps(
            {
                "ok": False,
                "stop": True,
                "message": f"BLOCKED: {check['detail']}",
                "nextTool": "uiforgemax_preflight",
            },
            indent=2,
        )

    resolved = str(Path(project_root).resolve())
    session["workspaceRoot"] = resolved
    save_session(session)

    visible = check.get("visible", [])

    if run_id:
        state = ctx.store.load(run_id)
        state.project_root = resolved
        state.record(state.current_stage, "workspace_set", resolved)
        ctx.store.save(state)
        return json.dumps(
            {
                "ok": True,
                "message": f"Workspace set to {resolved} for run {run_id}.",
                "runId": run_id,
                "workspaceRoot": resolved,
                "workspaceContents": visible,
                "nextTool": "uiforgemax_add_prompt",
            },
            indent=2,
        )

    return json.dumps(
        {
            "ok": True,
            "message": f"Workspace set to {resolved}. Call uiforgemax_start_run to begin.",
            "workspaceRoot": resolved,
            "workspaceContents": visible,
            "nextTool": "uiforgemax_start_run",
        },
        indent=2,
    )


def add_workspace_root(ctx: ToolContext, run_id: str, name: str, path: str) -> str:
    """Register an additional repo root for a run (e.g. a separate UI/API repo).

    Use when the PLAN stage blocks because a create/modify action references a
    named root that isn't the primary `project_root` and hasn't been added
    yet. `name` must match what the plan action uses in its `"root"` field.
    """
    if name in ("default", ""):
        return json.dumps(
            {
                "ok": False,
                "stop": True,
                "message": "BLOCKED: 'default' is reserved for the primary project_root — choose another name.",
            },
            indent=2,
        )

    session = load_session()
    check = _check_workspace(path, session.get("ideWorkspaceFolders", []))
    if not check["ok"]:
        return json.dumps(
            {"ok": False, "stop": True, "message": f"BLOCKED: {check['detail']}"},
            indent=2,
        )

    resolved = str(Path(path).resolve())
    state = ctx.store.load(run_id)
    state.project_roots[name] = resolved
    state.record(state.current_stage, "workspace_root_added", f"{name}={resolved}")
    ctx.store.save(state)
    return json.dumps(
        {
            "ok": True,
            "runId": run_id,
            "message": f"Workspace root '{name}' set to {resolved}. Call uiforgemax_advance to continue.",
            "projectRoots": {"default": state.project_root, **state.project_roots},
            "workspaceContents": check.get("visible", []),
            "nextTool": "uiforgemax_advance",
        },
        indent=2,
    )


def _is_store_python_alias(exe: str | None) -> bool:
    """Microsoft Store ``WindowsApps\\python.exe`` stubs hang/timeout in subprocesses."""
    if not exe:
        return False
    normalized = str(exe).replace("/", "\\").lower()
    return "windowsapps" in normalized or "pythonsoftwarefoundation.python" in normalized


def _resolve_python_executable(preferred: str | None) -> str:
    """Pick a usable python: path or PATH command; prefer non-Store over MCP venv."""
    from uiforgemax.graphify.cli_runner import resolve_python_spec

    candidates: list[str] = []
    if preferred:
        resolved = resolve_python_spec(preferred)
        if resolved:
            candidates.append(resolved)
        else:
            candidates.append(preferred)
    candidates.append(sys.executable)
    # Prefer the project venv if MCP somehow started on Store python.
    try:
        venv = Path(__file__).resolve().parents[3] / ".venv" / "Scripts" / "python.exe"
        if venv.exists():
            candidates.append(str(venv))
    except Exception:  # noqa: BLE001
        pass

    for cand in candidates:
        if not cand:
            continue
        resolved = resolve_python_spec(cand) or (str(Path(cand).resolve()) if Path(cand).exists() else None)
        if resolved and not _is_store_python_alias(resolved):
            return resolved
    # Last resort: MCP process python even if Store (better than inventing a path).
    return sys.executable


def _check_python_executable(exe: str) -> dict[str, Any]:
    path = Path(exe)
    if _is_store_python_alias(exe):
        return {
            "name": "PYTHON_EXECUTABLE",
            "ok": False,
            "detail": (
                f"Microsoft Store Python alias is unreliable: {exe}. "
                "Use the project venv, e.g. …/UiforgeMax/.venv/Scripts/python.exe"
            ),
        }
    if not path.exists():
        return {"name": "PYTHON_EXECUTABLE", "ok": False, "detail": f"Not found: {exe}"}
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = (proc.stdout or proc.stderr or "").strip()
        return {"name": "PYTHON_EXECUTABLE", "ok": proc.returncode == 0, "detail": f"{exe} → {version}"}
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"name": "PYTHON_EXECUTABLE", "ok": False, "detail": str(exc)}


def _check_graphify_in_process() -> dict[str, Any]:
    """Require the real Graphify package (PyPI ``graphifyy`` → ``import graphify``)."""
    try:
        import graphify  # noqa: F401
        from uiforgemax.graphify.cli_runner import ensure_graphify_available, graphify_python

        # Prefer a python that can import graphify (MCP venv), not a Store alias.
        py = graphify_python()
        # In-process import already proved graphify is loadable. When the resolved
        # CLI python is this same interpreter, skip a second subprocess probe —
        # that probe was adding ~10s hangs under OneDrive/MCP stdio and could
        # false-fail when stdout was empty despite exit 0.
        try:
            same_interpreter = Path(py).resolve() == Path(sys.executable).resolve()
        except OSError:
            same_interpreter = py == sys.executable
        if same_interpreter:
            return {
                "name": "GRAPHIFY_MODULE",
                "ok": True,
                "detail": (
                    f"real graphify CLI OK via {py} — "
                    f"uses `python -m graphify update/query`"
                ),
            }
        check = ensure_graphify_available(py)
        ok = bool(check.get("ok"))
        detail_extra = check.get("detail") or ""
        return {
            "name": "GRAPHIFY_MODULE",
            "ok": ok,
            "detail": (
                f"real graphify CLI {'OK' if ok else 'FAILED'} via {py} — "
                f"{detail_extra or 'uses `python -m graphify update/query`'}"
            ),
        }
    except ImportError as exc:
        return {
            "name": "GRAPHIFY_MODULE",
            "ok": False,
            "detail": f"Real graphify not installed ({exc}). pip install graphifyy",
        }


def _check_graphify_subprocess(exe: str) -> dict[str, Any]:
    if _is_store_python_alias(exe):
        return {
            "name": "GRAPHIFY_SUBPROCESS",
            "ok": False,
            "detail": f"skipped Store Python alias (hangs): {exe}",
        }
    try:
        proc = subprocess.run(
            [exe, "-c", "import graphify; print('ok')"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Exit 0 is enough — do not require stdout "ok" (can be empty under MCP stdio).
        ok = proc.returncode == 0
        return {
            "name": "GRAPHIFY_SUBPROCESS",
            "ok": ok,
            "detail": (proc.stdout or "").strip()
            or (proc.stderr or "").strip()
            or f"exit {proc.returncode}",
        }
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"name": "GRAPHIFY_SUBPROCESS", "ok": False, "detail": str(exc)}


def _list_top_level(path: Path, limit: int = 40) -> list[str]:
    """Cheap, non-recursive listing so a human can point instead of typing exact
    paths — e.g. confirming "ui" + "backend" are both right there already."""
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return []
    return [f"{p.name}/" if p.is_dir() else p.name for p in entries[:limit]]


def _is_within_workspace_folders(path: Path, folders: list[str]) -> bool:
    resolved = path.resolve()
    for folder in folders:
        try:
            f = Path(folder).resolve()
        except OSError:
            continue
        if resolved == f or f in resolved.parents:
            return True
    return False


def _check_workspace(
    project_root: str,
    workspace_folders: list[str] | None = None,
    check_name: str = "WORKSPACE",
) -> dict[str, Any]:
    path = Path(project_root)
    if not path.exists():
        return {"name": check_name, "ok": False, "detail": f"Path does not exist: {project_root}"}
    if not path.is_dir():
        return {"name": check_name, "ok": False, "detail": f"Not a directory: {project_root}"}
    if workspace_folders and not _is_within_workspace_folders(path, workspace_folders):
        return {
            "name": check_name,
            "ok": False,
            "detail": (
                f"{path.resolve()} is not inside any currently-open IDE workspace folder "
                f"({', '.join(workspace_folders)}). Open it in the IDE workspace, or pass the "
                "matching workspace_folders, before using this path."
            ),
        }
    visible = _list_top_level(path)
    empty = not visible
    return {
        "name": check_name,
        "ok": True,
        "detail": f"{path.resolve()} ({'empty — greenfield OK' if empty else 'has files'})",
        "visible": visible,
    }


def _parse_json_object(raw: str | None) -> tuple[dict[str, str], str | None]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object of {name: path}")
        return {str(k): str(v) for k, v in parsed.items()}, None
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, str(exc)


def _parse_json_array(raw: str | None) -> tuple[list[str], str | None]:
    if not raw:
        return [], None
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("expected a JSON array of folder paths")
        return [str(p) for p in parsed], None
    except (json.JSONDecodeError, ValueError) as exc:
        return [], str(exc)
