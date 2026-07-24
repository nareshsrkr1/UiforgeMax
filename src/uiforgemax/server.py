"""UiForgeMax MCP server (stdio).

Registers the state-gated tool surface. Handler exceptions are converted to
readable text so the driving agent sees a clear ``BLOCKED``/config message
instead of a transport error — but the underlying state is never mutated past a
failed gate.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from uiforgemax.config import Config, redact
from uiforgemax.mcp_response import tool_response
from uiforgemax.pipeline_guide import get_pipeline_guide
from uiforgemax.state.gates import GateError
from uiforgemax.tools import ToolContext
from uiforgemax.tools import approvals, clarifications, inputs, lifecycle, mediation, pipeline, preflight

mcp = FastMCP("uiforgemax")
_config = Config.from_env()
_ctx = ToolContext.from_config(_config)


def _run_id_from_call(args: tuple, kwargs: dict) -> str | None:
    """Handlers are ``(ctx, run_id, …)`` or ``run_id=`` kwarg."""
    if isinstance(kwargs.get("run_id"), str):
        return kwargs["run_id"]
    if len(args) >= 2 and isinstance(args[1], str):
        return args[1]
    return None


def _guard(fn, *args, **kwargs) -> str:
    """Run a handler, converting known errors into readable tool output."""
    try:
        return fn(*args, **kwargs)
    except GateError as e:
        msg = redact(str(e))
        run_id = _run_id_from_call(args, kwargs)
        if run_id:
            try:
                state = _ctx.store.load(run_id)
                return tool_response(
                    state,
                    msg,
                    stop=True,
                    jira_configured=_config.jira.is_configured,
                )
            except Exception:  # noqa: BLE001 - fall back to plain text
                pass
        return msg
    except FileNotFoundError as e:
        return redact(f"BLOCKED: {e}")
    except ValueError as e:
        return redact(f"ERROR: {e}")
    except Exception as e:  # noqa: BLE001 - surface unexpected errors safely
        return redact(f"ERROR ({type(e).__name__}): {e}")

# --- Lifecycle -------------------------------------------------------------

@mcp.tool()
def uiforgemax_preflight(
    project_root: str | None = None,
    python_executable: str | None = None,
    components: str | dict | None = None,
    workspace_folders: str | list | None = None,
) -> str:
    """Verify Python, built-in Graphify module, and optional workspace(s) before any run.

    Call this first in every agent session. Saves session.json (python path + workspace)
    under %%APPDATA%%/UiForgeMax/ for the entire session.

    `project_root` is the primary/default workspace. If the request spans multiple
    components (e.g. a separate `ui` and `backend` repo), pass all of them upfront via
    `components` as a JSON object string, e.g. '{"ui": "C:/Project/ui", "backend":
    "C:/Project/backend"}' — each gets its own Graphify artifacts later, not just the
    default root. `workspace_folders` is an optional JSON array string of the folder
    paths the IDE currently has open (e.g. '["C:/Project"]'); when passed, every path
    (default + each component) must resolve inside one of those folders or it BLOCKS.
    """
    return _guard(preflight.preflight, _ctx, project_root, python_executable, components, workspace_folders)


@mcp.tool()
def uiforgemax_set_workspace(project_root: str, run_id: str | None = None) -> str:
    """Set or update the workspace (project_root) for the session and optionally an active run."""
    return _guard(preflight.set_workspace, _ctx, project_root, run_id)


@mcp.tool()
def uiforgemax_add_workspace_root(run_id: str, name: str, path: str) -> str:
    """Register an extra repo root for a run (e.g. a second, unrelated repo).

    Use when the PLAN stage blocks with a "missing workspace root" message —
    pass the same `name` it names, and the folder path to register it. The
    primary workspace from start_run/preflight is always named "default";
    use this only for additional repos a plan action references via its own
    "root" field.
    """
    return _guard(preflight.add_workspace_root, _ctx, run_id, name, path)


@mcp.tool()
def uiforgemax_start_run(
    project_root: str | None = None,
    policy: str | None = None,
    components: str | dict | None = None,
    mode: str | None = "start",
    issue_key: str | None = None,
) -> str:
    """Start or resume a UiForgeMax run.

    `mode`:
    - `start` (default) — **clean slate**: archive prior incomplete runs for this
      workspace, ignore stale session component roots, create a new run id.
    - `resume` — continue the latest non-terminal run for this workspace
      (optionally filtered by `issue_key`). Does not create a new run.

    Optional `policy` override (frontend_first | full_stack | extend_existing |
    contract_driven). When omitted, policy stays `pending` until classify / mediation
    chooses one.

    When the user names a ticket (e.g. SCRUM-5), pass `issue_key` so intake
    `nextTool` becomes `uiforgemax_add_jira` — never invent the ticket via add_prompt.

    `components` (optional JSON object string, e.g. '{"ui": "...", "backend": "..."}')
    registers additional named roots for this run — used on mode=start only."""
    return _guard(lifecycle.start_run, _ctx, project_root, policy, components, mode, issue_key)


@mcp.tool()
def uiforgemax_resume_run(
    project_root: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
) -> str:
    """Resume an existing non-terminal run (by run_id, or latest match for workspace/issue).

    Prefer this (or start_run(mode='resume')) when the user says \"resume\".
    For a clean slate, use start_run(mode='start') instead."""
    return _guard(lifecycle.resume_run, _ctx, project_root, issue_key, run_id)


@mcp.tool()
def uiforgemax_get_run_status(run_id: str) -> str:
    """Return the full run state (status, stage, inputs, approvals, artifacts)."""
    return _guard(lifecycle.get_run_status, _ctx, run_id)


@mcp.tool()
def uiforgemax_list_runs() -> str:
    """List runs with status/workspace/issueKey and whether each is resumable."""
    return _guard(lifecycle.list_runs, _ctx)


@mcp.tool()
def uiforgemax_cancel_run(run_id: str, reason: str | None = None) -> str:
    """Cancel a run."""
    return _guard(lifecycle.cancel_run, _ctx, run_id, reason)


# --- Inputs (Stage 0) ------------------------------------------------------

@mcp.tool()
def uiforgemax_add_jira(run_id: str, issue_key: str) -> str:
    """Register a Jira issue as input (fetched read-only in Stage 0)."""
    return _guard(inputs.add_jira, _ctx, run_id, issue_key)


@mcp.tool()
def uiforgemax_add_prompt(run_id: str, text: str) -> str:
    """Add a free-text prompt requirement."""
    return _guard(inputs.add_prompt, _ctx, run_id, text)


@mcp.tool()
def uiforgemax_add_html(run_id: str, html: str) -> str:
    """Add an HTML snippet/page as input."""
    return _guard(inputs.add_html, _ctx, run_id, html)


@mcp.tool()
def uiforgemax_add_image(run_id: str, source_path: str, role: str = "reference") -> str:
    """Add a wireframe/mock/screenshot image with a role.

    role: reference | before | after | wireframe | mockup. Use before/after for
    redesign requests so the IDE model can diff them during classification.
    """
    return _guard(inputs.add_image, _ctx, run_id, source_path, role)


# --- Pipeline & approvals --------------------------------------------------

@mcp.tool()
def uiforgemax_advance(run_id: str) -> str:
    """Run the automatic zone until the next human gate or a terminal state.
    Prefer this over calling individual stage tools."""
    return _guard(pipeline.advance, _ctx, run_id)


@mcp.tool()
def uiforgemax_approve_api(run_id: str, by: str = "user") -> str:
    """Approve Gate 1 (API) — only valid when awaiting API approval."""
    return _guard(approvals.approve_api, _ctx, run_id, by)


@mcp.tool()
def uiforgemax_approve_understanding(run_id: str, by: str = "user") -> str:
    """Approve Gate 2 (Understanding)."""
    return _guard(approvals.approve_understanding, _ctx, run_id, by)


@mcp.tool()
def uiforgemax_approve_plan(run_id: str, by: str = "user") -> str:
    """Approve Gate 3 (Plan)."""
    return _guard(approvals.approve_plan, _ctx, run_id, by)


@mcp.tool()
def uiforgemax_request_changes(
    run_id: str,
    feedback: str,
    subtask_ids: list[str] | None = None,
) -> str:
    """Reject at a gate and delta-revise with feedback (no full restart).
    Optional subtask_ids to scope the delta to specific sub-tasks only."""
    return _guard(approvals.request_changes, _ctx, run_id, feedback, subtask_ids=subtask_ids)


@mcp.tool()
def uiforgemax_get_pipeline_guide(run_id: str | None = None) -> str:
    """Return detailed stage/tool reference and optional run stats.json for a run."""
    stats = None
    if run_id:
        stats_path = _ctx.store.run_dir(run_id) / "stats.json"
        if stats_path.exists():
            import json

            stats = json.loads(stats_path.read_text(encoding="utf-8"))
    return get_pipeline_guide(stats)


@mcp.tool()
def uiforgemax_answer_clarifications(run_id: str, answers: str) -> str:
    """Answer graph clarifications (JSON). IDE agent relays human answers."""
    return _guard(clarifications.answer_clarifications, _ctx, run_id, answers)


@mcp.tool()
def uiforgemax_submit_mediation(
    run_id: str,
    mediation_key: str,
    payload: str = "{}",
    payload_file: str | None = None,
) -> str:
    """Submit IDE model mediation JSON for the pending stage (Cursor/VS Code — no MCP LLM keys).

    For large payloads (plans with full file contents), write the JSON to a file
    and pass the path via `payload_file` (absolute, or relative to the run dir).
    Alternatively, set payload to 'file:<path>' to read from that path.
    When payload_file is set, the payload string parameter is ignored."""
    return _guard(mediation.submit_mediation, _ctx, run_id, mediation_key, payload, payload_file)


def main() -> None:
    report = _config.validate()
    print("UiForgeMax MCP starting. Config:", file=sys.stderr)
    print(report.render(), file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
