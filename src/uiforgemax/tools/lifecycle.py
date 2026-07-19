"""Run lifecycle tools: start, resume, status, cancel."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from uiforgemax.intake_hints import normalize_issue_key
from uiforgemax.mcp_response import tool_response
from uiforgemax.session import load_session, save_session, workspace_root
from uiforgemax.state import RunState, Status
from uiforgemax.tools.context import ToolContext
from uiforgemax.tools.preflight import _check_workspace, _list_top_level, _parse_json_object


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _norm_path(path: str | Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").lower()


def _run_issue_key(ctx: ToolContext, run_id: str) -> str | None:
    jira = ctx.store.run_dir(run_id) / "inputs" / "jira.raw.json"
    if not jira.exists():
        state = ctx.store.load(run_id)
        return (state.inputs.get("jira") or {}).get("issueKey")
    try:
        data = json.loads(jira.read_text(encoding="utf-8"))
        return data.get("key")
    except (OSError, json.JSONDecodeError):
        return None


def _find_resumable_runs(
    ctx: ToolContext,
    *,
    project_root: str | None = None,
    issue_key: str | None = None,
) -> list[RunState]:
    """Newest-first non-terminal runs matching workspace and/or issue key."""
    matches: list[RunState] = []
    root_norm = _norm_path(project_root) if project_root else None
    issue = (issue_key or "").strip().upper() or None
    for run_id in reversed(ctx.store.list_runs()):
        try:
            state = ctx.store.load(run_id)
        except Exception:  # noqa: BLE001
            continue
        if state.status in Status.terminal():
            continue
        if root_norm and state.project_root and _norm_path(state.project_root) != root_norm:
            continue
        if issue:
            found = (_run_issue_key(ctx, run_id) or "").upper()
            if found != issue:
                continue
        matches.append(state)
    return matches


def _archive_prior_runs(
    ctx: ToolContext,
    *,
    project_root: str,
    keep_run_id: str | None = None,
) -> list[str]:
    """Archive non-terminal runs for this workspace so ``start`` is a clean slate."""
    archived: list[str] = []
    root_norm = _norm_path(project_root)
    for run_id in list(ctx.store.list_runs()):
        if keep_run_id and run_id == keep_run_id:
            continue
        try:
            state = ctx.store.load(run_id)
        except Exception:  # noqa: BLE001
            continue
        if not state.project_root or _norm_path(state.project_root) != root_norm:
            continue
        # Archive incomplete runs; leave completed history unless same active session clutter.
        if state.status in Status.terminal() and state.status != Status.CANCELLED:
            continue
        dest = ctx.store.archive_run(run_id, reason="superseded by uiforgemax_start_run(mode=start)")
        if dest:
            archived.append(run_id)
    return archived


def start_run(
    ctx: ToolContext,
    project_root: str | None = None,
    policy: str | None = None,
    components: str | None = None,
    mode: str | None = "start",
    issue_key: str | None = None,
) -> str:
    """Start a fresh run, or resume an existing one.

    ``mode``:
    - ``start`` (default) — clean slate: archive prior incomplete runs for this
      workspace, do **not** reuse session component roots, create a new run id.
    - ``resume`` — find the latest non-terminal run for this workspace (and
      optional ``issue_key``) and continue it; do not create a new run.
    """
    mode_norm = (mode or "start").strip().lower()
    if mode_norm not in {"start", "resume"}:
        return json.dumps(
            {
                "ok": False,
                "stop": True,
                "message": f"BLOCKED: mode must be 'start' or 'resume' (got {mode!r}).",
                "nextTool": "uiforgemax_start_run",
            },
            indent=2,
        )

    if mode_norm == "resume":
        return resume_run(ctx, project_root=project_root, issue_key=issue_key, run_id=None)

    resolved_root = project_root or workspace_root()
    if not resolved_root:
        return json.dumps(
            {
                "ok": False,
                "stop": True,
                "message": (
                    "BLOCKED: workspace (project_root) not set. "
                    "Run uiforgemax_preflight(project_root='C:/path/to/workspace') or "
                    "uiforgemax_set_workspace(project_root='...') first."
                ),
                "nextTool": "uiforgemax_preflight",
                "blockedTools": ["uiforgemax_advance", "uiforgemax_add_jira"],
            },
            indent=2,
        )

    root_path = Path(resolved_root)
    if not root_path.exists() or not root_path.is_dir():
        return json.dumps(
            {
                "ok": False,
                "stop": True,
                "message": f"BLOCKED: workspace invalid: {resolved_root}",
                "nextTool": "uiforgemax_preflight",
            },
            indent=2,
        )

    session = load_session()
    ide_folders = session.get("ideWorkspaceFolders", [])

    components_map, components_error = _parse_json_object(components)
    if components and components_error:
        return json.dumps(
            {"ok": False, "stop": True, "message": f"BLOCKED: components: {components_error}"},
            indent=2,
        )

    # Fresh start: only roots explicitly passed this call — never merge stale session roots.
    resolved_roots: dict[str, str] = {}
    for name, path in components_map.items():
        check = _check_workspace(path, ide_folders, check_name=f"COMPONENT:{name}")
        if not check["ok"]:
            return json.dumps(
                {
                    "ok": False,
                    "stop": True,
                    "message": f"BLOCKED: component '{name}': {check['detail']}",
                    "nextTool": "uiforgemax_preflight",
                },
                indent=2,
            )
        resolved_roots[name] = str(Path(path).resolve())

    archived = _archive_prior_runs(ctx, project_root=str(root_path.resolve()))

    workspace_source = "explicit" if project_root else "reused_from_previous_session"
    run_id = _new_run_id()
    # Avoid colliding with an active or archived id in the same second.
    archive_root = Path(ctx.config.runs_root) / "_archive"
    suffix = 0
    while ctx.store.exists(run_id) or (archive_root / run_id).exists():
        suffix += 1
        run_id = f"{_new_run_id()}-{suffix}"

    chosen_policy = policy or ctx.config.default_policy or "pending"
    state = ctx.store.create(
        run_id,
        project_root=str(root_path.resolve()),
        policy=chosen_policy,
        project_roots=resolved_roots,
    )

    pending_key = normalize_issue_key(issue_key)
    if pending_key:
        state.inputs["pendingIssueKey"] = pending_key
        ctx.store.save(state)

    # Reset session run pointers for a clean start; keep pythonExecutable / graphifyReady.
    session["workspaceRoot"] = str(root_path.resolve())
    session["activeRunId"] = run_id
    session["projectRoots"] = dict(resolved_roots)  # replace, do not merge
    session.pop("lastResumedRunId", None)
    save_session(session)

    msg = (
        f"Started NEW run '{run_id}' (mode=start). "
        f"project_root={root_path.resolve()} policy={chosen_policy}. "
        f"Archived {len(archived)} prior incomplete run(s) for this workspace. "
        f"Temp artifacts: {ctx.store.run_dir(run_id)}"
    )
    if pending_key:
        msg += (
            f" Pending issue '{pending_key}' — call "
            f"uiforgemax_add_jira(run_id='{run_id}', issue_key='{pending_key}') next; "
            "do not invent the ticket via add_prompt."
        )
    elif ctx.config.jira.is_configured:
        msg += (
            " Jira is configured — prefer uiforgemax_add_jira for ticket keys "
            "(SCRUM-5, PROJ-123); use add_prompt only for free-text requirements."
        )
    extra: dict = {
        "mode": "start",
        "runsRoot": ctx.config.runs_root,
        "workspaceRoot": str(root_path.resolve()),
        "workspaceSource": workspace_source,
        "workspaceContents": _list_top_level(root_path),
        "projectRoots": resolved_roots,
        "archivedRuns": archived,
        "pythonExecutable": session.get("pythonExecutable"),
        "graphifyModule": session.get("graphifyModule", "uiforgemax.graphify"),
        "note": (
            "mode=start is a clean slate. Use uiforgemax_start_run(mode='resume') or "
            "uiforgemax_resume_run to continue an existing run."
        ),
        "jiraConfigured": ctx.config.jira.is_configured,
    }
    if pending_key:
        extra["suggestedIssueKey"] = pending_key
    if workspace_source == "reused_from_previous_session":
        extra["warning"] = (
            f"No project_root passed to start_run — reused workspace from session: "
            f"{root_path.resolve()}. Prefer passing project_root explicitly."
        )
    return tool_response(
        state,
        msg,
        extra=extra,
        jira_configured=ctx.config.jira.is_configured,
    )


def resume_run(
    ctx: ToolContext,
    project_root: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
) -> str:
    """Resume an existing non-terminal run (by id, or latest match for workspace/issue)."""
    session = load_session()
    resolved_root = project_root or workspace_root()

    if run_id:
        if not ctx.store.exists(run_id):
            return json.dumps(
                {
                    "ok": False,
                    "stop": True,
                    "message": f"BLOCKED: run '{run_id}' not found. Use list_runs or start_run(mode='start').",
                    "nextTool": "uiforgemax_list_runs",
                },
                indent=2,
            )
        state = ctx.store.load(run_id)
        if state.status in Status.terminal():
            return json.dumps(
                {
                    "ok": False,
                    "stop": True,
                    "message": (
                        f"BLOCKED: run '{run_id}' is terminal ({state.status.value}). "
                        "Call start_run(mode='start') for a new run."
                    ),
                    "nextTool": "uiforgemax_start_run",
                },
                indent=2,
            )
    else:
        matches = _find_resumable_runs(ctx, project_root=resolved_root, issue_key=issue_key)
        if not matches and issue_key and resolved_root:
            # Fallback: match issue only, then workspace only.
            matches = _find_resumable_runs(ctx, project_root=None, issue_key=issue_key)
        if not matches and resolved_root:
            matches = _find_resumable_runs(ctx, project_root=resolved_root, issue_key=None)
        if not matches:
            return json.dumps(
                {
                    "ok": False,
                    "stop": True,
                    "message": (
                        "BLOCKED: no resumable run found"
                        + (f" for issue {issue_key}" if issue_key else "")
                        + (f" under {resolved_root}" if resolved_root else "")
                        + ". Call uiforgemax_start_run(mode='start') to begin fresh."
                    ),
                    "nextTool": "uiforgemax_start_run",
                    "hint": "start_run(mode='start', project_root='...')",
                },
                indent=2,
            )
        state = matches[0]

    session["activeRunId"] = state.run_id
    session["lastResumedRunId"] = state.run_id
    if state.project_root:
        session["workspaceRoot"] = state.project_root
    save_session(session)

    run_dir = ctx.store.run_dir(state.run_id)
    msg = (
        f"Resumed run '{state.run_id}' (mode=resume). "
        f"status={state.status.value} stage={state.current_stage.value} "
        f"project_root={state.project_root}"
    )
    extra = {
        "mode": "resume",
        "runsDir": str(run_dir),
        "workspaceRoot": state.project_root,
        "issueKey": _run_issue_key(ctx, state.run_id),
        "projectRoots": state.project_roots,
        "note": "Continuing existing artifacts — not a clean slate.",
    }
    # Smart resume: load checkpoint and auto-continue from the saved stage.
    from uiforgemax.pipeline.resume_checkpoint import (
        load_resume_checkpoint,
        mark_checkpoint_resuming,
        should_auto_advance_on_resume,
    )
    from uiforgemax.pipeline.testing import load_install_wait

    checkpoint = load_resume_checkpoint(run_dir)
    wait = load_install_wait(run_dir)
    if checkpoint:
        extra["resumeCheckpoint"] = checkpoint
    if wait:
        extra["installWait"] = wait

    if should_auto_advance_on_resume(state, run_dir):
        mark_checkpoint_resuming(run_dir)
        # Continue pipeline immediately from saved stage (tests-only after install pause).
        from uiforgemax.tools.pipeline import advance

        advanced = advance(ctx, state.run_id)
        try:
            body = json.loads(advanced)
        except json.JSONDecodeError:
            return advanced
        body["mode"] = "resume"
        body["resumedFrom"] = {
            "status": checkpoint.get("status") or state.status.value,
            "stage": checkpoint.get("resumeStage") or state.current_stage.value,
            "pauseReason": checkpoint.get("pauseReason"),
            "completedStages": checkpoint.get("completedStages") or [],
        }
        body["message"] = (
            f"Resumed run '{state.run_id}' from checkpoint "
            f"({body['resumedFrom']['stage']}) and continued automatically.\n"
            + (body.get("message") or "")
        )
        return json.dumps(body, indent=2)

    if state.status == Status.AWAITING_USER_INSTALL or checkpoint.get("paused"):
        shell = (wait or checkpoint).get("shell") or ""
        stage = checkpoint.get("resumeStage") or "10_test"
        msg = (
            f"Resumed run '{state.run_id}' — paused at {stage} "
            f"({checkpoint.get('pauseReason') or state.status.value}).\n"
            f"Completed: {checkpoint.get('completedStages') or []}.\n"
            f"If install is still needed:\n```\n{shell}\n```\n"
            f"Say **continue** again or call uiforgemax_advance — nextTool=advance. "
            f"Do NOT start_run(mode='start')."
        )
        extra["resumeHint"] = {
            "say": "continue",
            "nextTool": "uiforgemax_advance",
            "runId": state.run_id,
            "stage": stage,
            "status": state.status.value,
            "completedStages": checkpoint.get("completedStages"),
        }
        extra["nextTool"] = "uiforgemax_advance"
    return tool_response(state, msg, extra=extra)


def get_run_status(ctx: ToolContext, run_id: str) -> str:
    state = ctx.store.load(run_id)
    run_path = ctx.store.run_dir(run_id)
    from uiforgemax.pipeline.resume_checkpoint import load_resume_checkpoint

    checkpoint = load_resume_checkpoint(run_path)
    summary = {
        "runId": state.run_id,
        "status": state.status.value,
        "currentStage": state.current_stage.value,
        "projectRoot": state.project_root,
        "runsDir": str(run_path),
        "inputs": state.inputs,
        "policy": state.policy,
        "compliance": state.compliance.model_dump(),
        "approvals": state.approvals.model_dump(),
        "artifacts": state.artifacts,
        "resumeCheckpoint": checkpoint or None,
    }
    extra: dict = {}
    # Attach gate packages so status checks also surface full approval detail.
    if state.status == Status.AWAITING_UNDERSTANDING_APPROVAL:
        from uiforgemax.pipeline.planning import build_understanding_approval_package

        extra["understandingApproval"] = build_understanding_approval_package(run_path)
    if state.status == Status.AWAITING_PLAN_APPROVAL:
        from uiforgemax.pipeline.planning import build_plan_approval_package

        extra["planApproval"] = build_plan_approval_package(run_path)
    if checkpoint:
        extra["resumeCheckpoint"] = checkpoint
    if state.status == Status.AWAITING_USER_INSTALL:
        from uiforgemax.pipeline.testing import load_install_wait

        extra["installWait"] = load_install_wait(run_path)
    return tool_response(state, json.dumps(summary, indent=2), extra=extra or None)


def cancel_run(ctx: ToolContext, run_id: str, reason: str | None = None) -> str:
    state = ctx.store.load(run_id)
    if state.status in Status.terminal():
        return tool_response(state, f"Run already terminal ({state.status.value}).", stop=True)
    state.status = Status.CANCELLED
    state.record(state.current_stage, "cancelled", reason)
    ctx.store.save(state)
    return tool_response(state, f"Run '{run_id}' cancelled.", stop=True)


def list_runs(ctx: ToolContext) -> str:
    rows = []
    for run_id in ctx.store.list_runs():
        try:
            state = ctx.store.load(run_id)
            rows.append(
                {
                    "runId": run_id,
                    "status": state.status.value,
                    "stage": state.current_stage.value,
                    "projectRoot": state.project_root,
                    "issueKey": _run_issue_key(ctx, run_id),
                    "resumable": state.status not in Status.terminal(),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append({"runId": run_id, "error": str(exc)})
    body = {
        "runs": rows,
        "runsRoot": ctx.config.runs_root,
        "activeRunId": load_session().get("activeRunId"),
        "note": (
            "start_run(mode='start') archives incomplete runs for the workspace and creates new. "
            "start_run(mode='resume') / resume_run continues the latest matching resumable run."
        ),
    }
    return json.dumps(body, indent=2)
