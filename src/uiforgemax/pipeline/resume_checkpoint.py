"""Persisted resume checkpoint — where the run paused and how to continue.

Written to ``resume-checkpoint.json`` (and mirrored on ``run.json`` artifacts)
so ``resume`` / ``continue`` always knows the exact stage without guessing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uiforgemax.state import RunState, Stage, Status

CHECKPOINT_PATH = "resume-checkpoint.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stages_completed_before(current: Stage) -> list[str]:
    order = Stage.ordered()
    try:
        idx = order.index(current)
    except ValueError:
        return []
    return [s.value for s in order[:idx]]


def write_resume_checkpoint(
    run_dir: Path,
    state: RunState,
    *,
    pause_reason: str,
    next_tool: str = "uiforgemax_advance",
    resume_action: str = "advance",
    skip_on_resume: dict[str, Any] | None = None,
    human_commands: list[dict[str, Any]] | None = None,
    shell: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Persist a machine-readable resume point for this run."""
    stage = state.current_stage
    payload = {
        "schemaVersion": 1,
        "runId": state.run_id,
        "updatedAt": _now(),
        "paused": True,
        "status": state.status.value,
        "currentStage": stage.value,
        "resumeStage": stage.value,
        "pauseReason": pause_reason,
        "completedStages": _stages_completed_before(stage),
        "pendingStages": [s.value for s in Stage.ordered() if s.value not in _stages_completed_before(stage)],
        "nextTool": next_tool,
        "resumeAction": resume_action,
        "projectRoot": state.project_root,
        "projectRoots": state.project_roots,
        "skipOnResume": skip_on_resume or {},
        "humanCommands": human_commands or [],
        "shell": shell or "",
        "notes": notes
        or (
            "Say 'resume' or 'continue' in chat. Agent calls uiforgemax_resume_run "
            "(or advance) — continues from resumeStage; does not start a new run."
        ),
        "userPhrases": ["resume", "continue", "done", "installed", "retry"],
    }
    path = run_dir / CHECKPOINT_PATH
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    state.artifacts["resumeCheckpoint"] = CHECKPOINT_PATH
    return payload


def load_resume_checkpoint(run_dir: Path) -> dict[str, Any]:
    path = run_dir / CHECKPOINT_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def clear_resume_checkpoint(run_dir: Path) -> None:
    path = run_dir / CHECKPOINT_PATH
    path.unlink(missing_ok=True)


def mark_checkpoint_resuming(run_dir: Path) -> dict[str, Any]:
    """Flip paused→resuming when the human/agent continues."""
    data = load_resume_checkpoint(run_dir)
    if not data:
        return {}
    data["paused"] = False
    data["resuming"] = True
    data["resumedAt"] = _now()
    path = run_dir / CHECKPOINT_PATH
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def should_auto_advance_on_resume(state: RunState, run_dir: Path) -> bool:
    """True when resume should immediately continue the pipeline."""
    if state.status == Status.AWAITING_USER_INSTALL:
        return True
    cp = load_resume_checkpoint(run_dir)
    if cp.get("paused") and cp.get("resumeAction") == "advance":
        return True
    return False
