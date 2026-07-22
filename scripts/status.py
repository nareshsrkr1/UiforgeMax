"""UiForgeMax status - what start/resume will actually do right now.

Run with the project venv's python so it imports the real uiforgemax package:
    .venv\\Scripts\\python.exe scripts\\status.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uiforgemax.config import Config
from uiforgemax.session import load_session, session_path
from uiforgemax.state import Status


def _fmt(v):
    return v if v not in (None, "", []) else "(none)"


def main() -> None:
    print("=== UiForgeMax Status ===\n")

    session = load_session()
    sp = session_path()
    print(f"Session file: {sp}")
    if not session:
        print("  No session found. uiforgemax_preflight has not been run yet.")
        print("  -> uiforgemax_start_run will BLOCK until preflight runs.\n")
        return

    print(f"  pythonExecutable : {_fmt(session.get('pythonExecutable'))}")
    print(f"  graphifyReady    : {_fmt(session.get('graphifyReady'))}")
    print(f"  workspaceRoot    : {_fmt(session.get('workspaceRoot'))}")
    print(f"  activeRunId      : {_fmt(session.get('activeRunId'))}")
    print(f"  lastResumedRunId : {_fmt(session.get('lastResumedRunId'))}")
    print(f"  updatedAt        : {_fmt(session.get('updatedAt'))}")
    print()

    cfg = Config.from_env()
    active_id = session.get("activeRunId")
    if not active_id:
        print("No active run recorded in session.")
        print("  -> uiforgemax_start_run(project_root=...) will START a NEW run.")
        print("  -> uiforgemax_resume_run will look for the latest non-terminal run")
        print("     matching workspaceRoot/issue_key, if any.\n")
        return

    from uiforgemax.tools import ToolContext

    ctx = ToolContext.from_config(cfg)
    run_dir = ctx.store.run_dir(active_id)
    if not ctx.store.exists(active_id):
        print(f"activeRunId '{active_id}' points at a run that no longer exists")
        print(f"  (expected at {run_dir}).")
        print("  This is a STALE session pointer - safe to ignore. The next")
        print("  start_run(mode='start') will create a fresh run; resume_run")
        print("  will fall back to searching by workspace/issue_key.\n")
        return

    state = ctx.store.load(active_id)
    terminal = state.status in Status.terminal()

    print(f"Active run: {active_id}")
    print(f"  status       : {state.status.value}{'  (TERMINAL)' if terminal else ''}")
    print(f"  currentStage : {state.current_stage.value}")
    print(f"  project_root : {_fmt(state.project_root)}")
    print(f"  updated_at   : {_fmt(state.updated_at)}")
    print()

    if terminal:
        print("This run is TERMINAL (completed/failed/cancelled).")
        print("  -> uiforgemax_resume_run(run_id=...) on this id will be BLOCKED.")
        print("  -> uiforgemax_start_run(mode='start') will archive it and create a new run.")
    else:
        print("This run is NOT terminal - it can be resumed.")
        print(f"  -> uiforgemax_resume_run (no run_id) will pick this run up if it")
        print(f"     matches the workspace/issue_key you pass.")
        print(f"  -> uiforgemax_start_run(mode='start') will ARCHIVE this run and")
        print(f"     start a clean one instead - use resume if you want to continue it.")

    print()
    print(f"Run artifacts: {run_dir}")


if __name__ == "__main__":
    main()
