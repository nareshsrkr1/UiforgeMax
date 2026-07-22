"""Visual validation — compare implementation against wireframe/mockup references.

Post-implementation check that triggers delta re-implementation for sub-tasks
that fall below the fidelity threshold. Only runs when visual references
(images/wireframes) exist in the run inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.state import RunState, Stage, Status


def should_run_visual_validation(run_dir: Path, state: RunState) -> bool:
    modes = state.inputs.get("modes", [])
    if "image" not in modes:
        return False
    visual_path = run_dir / "visual-spec.json"
    if not visual_path.exists():
        return False
    visual = json.loads(visual_path.read_text(encoding="utf-8"))
    return bool(visual.get("images") or visual.get("wireframes") or visual.get("mockups"))


def apply_visual_validation(run_dir: Path, validation_result: dict[str, Any]) -> dict[str, Any]:
    impl_dir = run_dir / "implementation"
    impl_dir.mkdir(parents=True, exist_ok=True)
    out_path = impl_dir / "visual-validation.json"
    out_path.write_text(json.dumps(validation_result, indent=2), encoding="utf-8")
    return validation_result


def prepare_delta_reimplementation(
    run_dir: Path,
    state: RunState,
    failed_subtasks: list[dict[str, Any]],
    *,
    attempt: int = 1,
) -> dict[str, Any]:
    failed_ids = [r.get("subtaskId", "?") for r in failed_subtasks]

    delta = {
        "version": 1,
        "attempt": attempt + 1,
        "failedSubtasks": [],
        "source": "visual_validation",
    }

    for r in failed_subtasks:
        delta["failedSubtasks"].append({
            "subtaskId": r.get("subtaskId"),
            "fidelityScore": r.get("fidelityScore", 0),
            "issues": r.get("issues", []),
            "fixInstructions": r.get("fixInstructions", ""),
            "affectedFiles": r.get("affectedFiles", []),
        })

    plans_dir = run_dir / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "visual-delta.json").write_text(
        json.dumps(delta, indent=2), encoding="utf-8"
    )

    state.approvals.plan.feedback = json.dumps({
        "type": "visual_delta",
        "subtaskIds": failed_ids,
        "attempt": attempt + 1,
        "instruction": (
            "Re-implement ONLY the listed sub-tasks to fix visual fidelity issues. "
            "Do not modify sub-tasks that passed validation."
        ),
    })

    for st_id in failed_ids:
        state.subtask_progress[st_id] = "needs_reimplementation"

    return delta
