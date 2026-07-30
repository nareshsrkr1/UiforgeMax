"""Visual validation — compare implementation against intake visual SoT.

Works for HTML references, wireframes, mockups, and images. Triggers delta
re-implementation for sub-tasks below the fidelity threshold.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.visual_sot import detect_visual_references
from uiforgemax.state import RunState

_BUTTON_RE = re.compile(r"<button[^>]*>\s*([^<{][^<]*?)\s*</button>", re.I)


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


def find_cross_view_button_leaks(
    run_dir: Path,
    project_roots: dict[str, Path],
) -> list[dict[str, Any]]:
    """Flag a subtask whose implementation renders a button that the HTML SoT
    attributes to a DIFFERENT render function than this subtask's own screen.

    Copy-fidelity checks only ask "does this text match the HTML somewhere" —
    a button that's textually correct but was copied from a sibling screen
    (e.g. the Console page's "Register a physical dataset" button leaking onto
    the My Datasets page) passes that check every time. This asks the other
    question: does it belong on THIS screen.

    Only engages when a subtask's ``visualRegion.regionId`` is the literal HTML
    render-function name (a ``viewButtonMap`` key from mechanical HTML
    extraction) — anything else (an invented region id, no visual-spec, no
    plan) is skipped rather than guessed at.
    """
    visual_spec_path = run_dir / "visual-spec.json"
    subtasks_path = run_dir / "plans" / "subtasks.json"
    plan_path = run_dir / "plans" / "approved-plan.json"
    if not (visual_spec_path.exists() and subtasks_path.exists() and plan_path.exists()):
        return []

    try:
        view_map: dict[str, list[str]] = (
            json.loads(visual_spec_path.read_text(encoding="utf-8")).get("viewButtonMap") or {}
        )
    except (OSError, json.JSONDecodeError):
        return []
    if not view_map:
        return []

    owner_by_label: dict[str, set[str]] = {}
    for func_name, labels in view_map.items():
        for label in labels:
            owner_by_label.setdefault(label.strip().lower(), set()).add(func_name)

    try:
        subtasks = json.loads(subtasks_path.read_text(encoding="utf-8")).get("subtasks", [])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    files_by_subtask: dict[str, list[dict[str, Any]]] = {}
    for action in (plan.get("create") or []) + (plan.get("modify") or []):
        st_id = action.get("subtaskId")
        if st_id:
            files_by_subtask.setdefault(st_id, []).append(action)

    violations: list[dict[str, Any]] = []
    for st in subtasks:
        region = st.get("visualRegion") or {}
        region_id = region.get("regionId")
        if not region_id or region_id not in view_map:
            continue  # not a literal render-function name — nothing to check
        st_id = st.get("id") or st.get("subtaskId")
        for action in files_by_subtask.get(st_id, []):
            path = action.get("path")
            root = project_roots.get(action.get("root") or "default") or project_roots.get("default")
            if not root or not path:
                continue
            try:
                text = (Path(root) / path).read_text(encoding="utf-8")
            except OSError:
                continue
            for m in _BUTTON_RE.finditer(text):
                label = m.group(1).strip()
                owners = owner_by_label.get(label.lower())
                if not owners:
                    continue  # not a literal SoT button label — out of scope
                if region_id not in owners:
                    violations.append(
                        {
                            "subtaskId": st_id,
                            "file": path,
                            "buttonLabel": label,
                            "expectedView": region_id,
                            "actualOwningViews": sorted(owners),
                        }
                    )
    return violations
