"""State-gating engine — the authoritative anti-deviation layer.

Every tool that changes pipeline state calls :func:`assert_gate` first. If the
run is not in a valid state for that tool, it raises :class:`GateError` with a
``BLOCKED`` message. This is what guarantees the agent cannot skip gates or run
stages out of order — no prompt is trusted for correctness.
"""

from __future__ import annotations

from collections.abc import Callable

from uiforgemax.state.run_state import RunState, Status


class GateError(Exception):
    """Raised when a tool is called in a state that does not permit it."""


# A precondition returns (ok, reason_if_not_ok).
Precondition = Callable[[RunState], tuple[bool, str]]


def _require_status(*allowed: Status) -> Precondition:
    def check(run: RunState) -> tuple[bool, str]:
        if run.status in allowed:
            return True, ""
        allowed_str = ", ".join(s.value for s in allowed)
        return False, f"requires status in [{allowed_str}] but is '{run.status.value}'"

    return check


def _understanding_approved(run: RunState) -> tuple[bool, str]:
    # Understanding is auto-approved; keep check for legacy callers.
    if run.approvals.understanding.approved or not run.approvals.understanding.required:
        return True, ""
    return False, "requires understanding approval"


def _plan_approved_and_reviewed(run: RunState) -> tuple[bool, str]:
    if not run.approvals.plan.approved:
        return False, "requires plan approval (sole human gate before implement)"
    verdict = (run.artifacts.get("planReview") or {}).get("verdict")
    if verdict != "pass":
        return False, f"requires plan-review verdict 'pass' (current: {verdict!r})"
    return True, ""


def _not_terminal(run: RunState) -> tuple[bool, str]:
    if run.status in Status.terminal():
        return False, f"run is terminal ('{run.status.value}')"
    return True, ""


def _approve_plan_ready(run: RunState) -> tuple[bool, str]:
    """Plan approval is the sole human gate — reject early calls with an action hint."""
    if run.status in (Status.PLAN_REVIEWED, Status.AWAITING_PLAN_APPROVAL):
        return True, ""
    if run.status == Status.AWAITING_MEDIATION:
        return False, (
            "called too early (still awaiting_mediation). "
            "Call uiforgemax_submit_mediation for the pending mediation key, then "
            "uiforgemax_advance until status is awaiting_plan_approval — only then approve_plan"
        )
    if run.status in (Status.INTAKE, Status.ARCH_DETECTED):
        return False, (
            "called too early (still intake). Prefer uiforgemax_add_jira for ticket keys, "
            "then uiforgemax_advance through planning before approve_plan"
        )
    return False, (
        f"requires status in [plan_reviewed, awaiting_plan_approval] but is '{run.status.value}'. "
        "Follow nextTool — do not call approve_plan until the plan gate is open"
    )


# Tool -> ordered list of preconditions. Absence means no state precondition
# (e.g. read-only status tools).
GATE_REQUIREMENTS: dict[str, list[Precondition]] = {
    # Inputs may only be added during intake.
    "uiforgemax_add_jira": [_require_status(Status.INTAKE, Status.ARCH_DETECTED)],
    "uiforgemax_add_prompt": [_require_status(Status.INTAKE, Status.ARCH_DETECTED)],
    "uiforgemax_add_html": [_require_status(Status.INTAKE, Status.ARCH_DETECTED)],
    "uiforgemax_add_image": [_require_status(Status.INTAKE, Status.ARCH_DETECTED)],
    # Planning requires an approved understanding.
    "uiforgemax_generate_plan": [_not_terminal, _understanding_approved],
    # Review requires a generated plan.
    "uiforgemax_review_plan": [_require_status(Status.PLAN_READY, Status.PLAN_REVIEWED)],
    # Implementation is the guarded step: approved plan AND passing review.
    "uiforgemax_implement": [_not_terminal, _plan_approved_and_reviewed],
    # Tests only after implementation has begun.
    "uiforgemax_run_tests": [_require_status(Status.IMPLEMENTING, Status.TESTING)],
    # Approvals must target the right waiting state.
    "uiforgemax_approve_api": [_require_status(Status.AWAITING_API_APPROVAL)],
    "uiforgemax_approve_understanding": [
        _require_status(Status.UNDERSTANDING_READY, Status.AWAITING_UNDERSTANDING_APPROVAL)
    ],
    "uiforgemax_approve_plan": [_approve_plan_ready],
    "uiforgemax_submit_mediation": [_require_status(Status.AWAITING_MEDIATION)],
}


def assert_gate(tool: str, run: RunState) -> None:
    """Raise :class:`GateError` if ``tool`` cannot run in the current state."""
    for check in GATE_REQUIREMENTS.get(tool, []):
        ok, reason = check(run)
        if not ok:
            raise GateError(
                f"BLOCKED: {tool} {reason}. "
                f"Current stage='{run.current_stage.value}', status='{run.status.value}'. "
                f"Resolve the pending step or gate before retrying."
            )
