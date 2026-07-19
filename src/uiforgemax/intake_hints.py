"""Intake routing helpers — prefer real Jira fetch over invented prompt text."""

from __future__ import annotations

import re

# Standard Jira/Linear-style keys: PROJ-123, SCRUM-5, ABC1-99
_ISSUE_KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9]+-\d+)\b", re.IGNORECASE)


def extract_leading_issue_key(text: str) -> str | None:
    """Return ISSUE-KEY if ``text`` begins with one (e.g. ``SCRUM-5: …``)."""
    if not text or not str(text).strip():
        return None
    m = _ISSUE_KEY_RE.match(str(text))
    if not m:
        return None
    return m.group(1).upper()


def normalize_issue_key(issue_key: str | None) -> str | None:
    raw = (issue_key or "").strip().upper()
    if not raw:
        return None
    if _ISSUE_KEY_RE.match(raw) and re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", raw):
        return raw
    # Allow bare keys passed to start_run without trailing junk
    m = re.fullmatch(r"([A-Z][A-Z0-9]+-\d+)", raw)
    return m.group(1) if m else None


def prefer_jira_intake(*, jira_configured: bool, pending_issue_key: str | None) -> bool:
    """True when intake should steer the agent to ``uiforgemax_add_jira``."""
    if pending_issue_key:
        return True
    return bool(jira_configured)
