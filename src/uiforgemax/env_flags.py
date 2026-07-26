"""Shared UIFORGEMAX_* boolean env flags."""

from __future__ import annotations

import os


def _truthy(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")


def skip_mediation() -> bool:
    return _truthy("UIFORGEMAX_SKIP_MEDIATION")


def skip_plan_approval() -> bool:
    """Local/CI only: auto-approve the human plan gate."""
    return _truthy("UIFORGEMAX_SKIP_PLAN_APPROVAL")


def runs_retention_days() -> int:
    """Days to keep run dirs under ``UIFORGEMAX_RUNS_ROOT`` (default 14; ``0`` disables).

    See :func:`uiforgemax.pipeline.runs_prune.runs_retention_days`.
    """
    from uiforgemax.pipeline.runs_prune import runs_retention_days as _days

    return _days()


def install_timeout_seconds(default: int = 900) -> int:
    """Max seconds for dependency installs (npm/pip/…). Default 900 (15 minutes).

    Override with ``UIFORGEMAX_INSTALL_TIMEOUT``. On timeout MCP pauses the run
    (``awaiting_user_install``), writes handover/resume hints, and on the next
    ``uiforgemax_advance`` / ``resume_run`` retries tests without re-running a
    long auto-install.
    """
    raw = os.getenv("UIFORGEMAX_INSTALL_TIMEOUT", str(default)).strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return default
