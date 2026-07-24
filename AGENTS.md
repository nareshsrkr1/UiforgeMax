# UiForgeMax — Driving Agent

**MCP-only.** Use only `uiforgemax_*` tools. If MCP is not connected, **stop** — do not use other tools.

## Every session

1. Enable MCP `uiforgemax` in Cursor Settings → MCP.
2. Use **Agent** mode (default agent with this rule).
3. First tool: **`uiforgemax_preflight(project_root='…')`** — verifies Python + Graphify + workspace; saves session.
4. Then **`uiforgemax_start_run(mode='start', issue_key='…')`** for a clean slate (pass
   `issue_key` when the user named a ticket), or **`mode='resume'`** /
   **`uiforgemax_resume_run`** to continue an existing run.
5. For tickets: **`uiforgemax_add_jira`** — never invent the issue via `add_prompt`.
6. At plan gate: **wait for the human** (`waitForHuman`) — do not auto-`approve_plan`.
   Local/CI: `UIFORGEMAX_SKIP_PLAN_APPROVAL=1`.

Follow `nextTool` in each JSON response. Do not edit project files directly — MCP implements after the sole plan approval.

At **`awaiting_mediation`**, use your IDE model + `uiforgemax_submit_mediation`.
The response's `modelMediation.artifactContents` inlines the small artifacts — use
those directly, do **not** re-Read those files. Only Read a `readArtifacts` path
that is absent from `artifactContents` (large files like HTML / source snapshots).

See `agent/uiforgemax-agent.md` for the full tool allowlist and workflow — enforced every
session via `.cursor/rules/uiforgemax-agent.mdc` (`alwaysApply: true`), not just this link.
