"""Visual validation — compare implementation against intake visual SoT.

Works for HTML references, wireframes, mockups, and images. Triggers delta
re-implementation for sub-tasks below the fidelity threshold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.visual_sot import detect_visual_references
from uiforgemax.state import RunState


def should_run_visual_validation(run_dir: Path, state: RunState) -> bool:
    """True when any visual SoT exists: HTML, image, wireframe, or mockup."""
    ref = detect_visual_references(run_dir, state)
    if ref.get("hasVisualRef"):
        return True
    # Fallback: visual-spec already derived from HTML / images
    visual_path = run_dir / "visual-spec.json"
    if not visual_path.exists():
        return False
    try:
        visual = json.loads(visual_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        visual.get("images")
        or visual.get("wireframes")
        or visual.get("mockups")
        or visual.get("htmlDerived")
        or visual.get("referenceHtml")
        or visual.get("referenceImage")
    )


def apply_visual_validation(run_dir: Path, validation_result: dict[str, Any]) -> dict[str, Any]:
    impl_dir = run_dir / "implementation"
    impl_dir.mkdir(parents=True, exist_ok=True)
    out_path = impl_dir / "visual-validation.json"
    out_path.write_text(json.dumps(validation_result, indent=2), encoding="utf-8")
    return validation_result


def clear_visual_delta(run_dir: Path, *, reason: str = "done") -> None:
    """Archive plans/visual-delta.json so it cannot lock later full re-plans.

    GATE_PLAN / PLAN_REFINEMENT treat any existing visual-delta.json as an active
    delta-scope lock. After the visual cycle ends (pass, max attempts) or a
    non-visual rewind (post-implement / request_changes), the file must not linger
    — otherwise create/modify without the old failed ST-* tags get rejected.
    """
    path = run_dir / "plans" / "visual-delta.json"
    if not path.exists():
        return
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)[:48] or "done"
    archive = run_dir / "plans" / f"visual-delta.superseded-{safe}.json"
    plans = run_dir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    try:
        path.replace(archive)
    except OSError:
        path.unlink(missing_ok=True)


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
        # VISUAL_VALIDATION's outputSchema asks the model for "fidelity", not
        # "fidelityScore" — reading only the latter meant this always defaulted
        # to 0 even when the model correctly reported a real score.
        delta["failedSubtasks"].append({
            "subtaskId": r.get("subtaskId"),
            "fidelityScore": r.get("fidelityScore", r.get("fidelity", 0)),
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
            "Re-implement ONLY the listed sub-tasks to fix visual fidelity issues "
            "against the intake SoT (HTML / wireframe / mockup / image). "
            "Do not modify sub-tasks that passed validation."
        ),
    })

    for st_id in failed_ids:
        state.subtask_progress[st_id] = "needs_reimplementation"

    return delta
