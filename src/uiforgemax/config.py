"""Configuration loading and startup validation.

Secrets are read only from environment variables. They never live in
``run.json``, artifacts, logs, or the repo. All log/error rendering goes
through :func:`redact` so tokens cannot leak.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from uiforgemax.paths import default_runs_root

# No forced default — policy is set by classification (or explicitly on start_run).
VALID_POLICIES = {"frontend_first", "full_stack", "extend_existing", "contract_driven"}

# Env var names that hold secrets — used for redaction.
_SECRET_ENV_VARS = ("JIRA_API_TOKEN",)


@dataclass
class JiraConfig:
    base_url: str | None = None
    email: str | None = None
    api_token: str | None = None

    @property
    def is_configured(self) -> bool:
        # Email is optional: Cloud uses email + token (Basic); Server/DC uses a
        # Personal Access Token alone (Bearer). Only base_url + token are required.
        return bool(self.base_url and self.api_token)

    @property
    def uses_bearer(self) -> bool:
        """Token-only (no email) → Bearer PAT auth (Jira Server/Data Center)."""
        return bool(self.api_token and not self.email)


@dataclass
class Config:
    jira: JiraConfig = field(default_factory=JiraConfig)
    # Optional override only. When unset, start_run leaves policy as "pending"
    # until classify / IDE mediation picks one.
    default_policy: str | None = None
    # Where run artifacts are written on the machine driving the pipeline.
    runs_root: str = ".uiforgemax/runs"

    @classmethod
    def from_env(cls) -> "Config":
        raw = os.getenv("UIFORGEMAX_DEFAULT_POLICY")
        policy = raw if raw in VALID_POLICIES else None
        return cls(
            jira=JiraConfig(
                base_url=os.getenv("JIRA_BASE_URL"),
                email=os.getenv("JIRA_EMAIL"),
                api_token=os.getenv("JIRA_API_TOKEN"),
            ),
            default_policy=policy,
            runs_root=os.getenv("UIFORGEMAX_RUNS_ROOT", str(default_runs_root())),
        )

    def validate(self) -> "ConfigReport":
        """Validate config without raising. The server always starts; tools that
        need a missing capability return a clear config error at call time
        rather than guessing or hallucinating."""
        checks: list[CheckResult] = []

        checks.append(
            CheckResult("JIRA_BASE_URL", bool(self.jira.base_url), self.jira.base_url or "(unset)")
        )
        # Email is optional; show which auth mode will be used.
        auth_mode = "Basic (email+token)" if self.jira.email else "Bearer (token only)"
        checks.append(
            CheckResult("JIRA_EMAIL", True, self.jira.email or "(unset — optional)")
        )
        checks.append(
            CheckResult("JIRA_API_TOKEN", bool(self.jira.api_token), _mask(self.jira.api_token))
        )
        checks.append(
            CheckResult("JIRA_AUTH_MODE", bool(self.jira.api_token), auth_mode)
        )

        checks.append(CheckResult("UIFORGEMAX_RUNS_ROOT", True, self.runs_root))
        checks.append(
            CheckResult(
                "UIFORGEMAX_DEFAULT_POLICY",
                True,
                self.default_policy or "(unset — classify will choose)",
            )
        )
        return ConfigReport(checks=checks)


@dataclass
class CheckResult:
    name: str
    ok: bool
    display: str


@dataclass
class ConfigReport:
    checks: list[CheckResult]

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "OK " if c.ok else "-- "
            lines.append(f"[{mark}] {c.name}: {c.display}")
        return "\n".join(lines)


def _mask(value: str | None) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def redact(text: str) -> str:
    """Remove any known secret values from a string before logging or returning."""
    result = text
    for var in _SECRET_ENV_VARS:
        secret = os.getenv(var)
        if secret and secret in result:
            result = result.replace(secret, f"****{secret[-4:]}" if len(secret) > 4 else "****")
    return result
