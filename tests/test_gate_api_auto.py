"""Tests for GATE_API auto-approve behavior."""

from __future__ import annotations

from uiforgemax.state import RunState, Status


def test_gate_api_auto_approves():
    """GATE_API must auto-approve — sole human gate is GATE_PLAN."""
    state = RunState(
        run_id="test-run",
        project_root="C:/tmp",
        status=Status.REQUIREMENT_MAPPED,
    )
    state.approvals.api.required = True
    state.approvals.api.approved = False

    from uiforgemax.stages.runner import _gate_api
    from unittest.mock import MagicMock

    ctx = MagicMock()
    result = _gate_api(ctx, state)

    assert state.approvals.api.approved is True
    assert state.approvals.api.by == "system-auto"
    assert result.stop is False
    assert "auto-passed" in result.message.lower() or "auto" in result.message.lower()


def test_gate_api_already_approved():
    state = RunState(
        run_id="test-run",
        project_root="C:/tmp",
        status=Status.REQUIREMENT_MAPPED,
    )
    state.approvals.api.required = True
    state.approvals.api.approved = True
    state.approvals.api.by = "user"

    from uiforgemax.stages.runner import _gate_api
    from unittest.mock import MagicMock

    ctx = MagicMock()
    result = _gate_api(ctx, state)

    assert state.approvals.api.approved is True
    assert result.stop is False


def test_gate_api_not_required():
    state = RunState(
        run_id="test-run",
        project_root="C:/tmp",
        status=Status.REQUIREMENT_MAPPED,
    )
    state.approvals.api.required = False
    state.approvals.api.approved = False

    from uiforgemax.stages.runner import _gate_api
    from unittest.mock import MagicMock

    ctx = MagicMock()
    result = _gate_api(ctx, state)

    assert result.stop is False
