# UiForgeMax — Driving Agent

**MCP orchestrates; the IDE writes code after plan approval.**

Drive the pipeline with `uiforgemax_*` tools for intake, Graphify, plan, gates,
visual/test verify. If MCP is not connected, **stop**.

**Before plan approval:** prefer Graphify evidence via `uiforgemax_advance` —
do not wander the target repo to invent the plan.

**After plan approval (`awaiting_ide_apply`):** implement **only**
`plans/approved-plan.json` / `ideApplyBrief` paths with IDE **Read / Edit / Write**,
then `uiforgemax_advance` **once**. At `doNotAdvanceUntilEdited`: edit first —
do not blind-advance. Do **not** follow a drifted draft plan or push file bodies
via MCP.

**Opaque / oversized tool results — do NOT stop:** call
`uiforgemax_get_run_status(run_id)` and continue with `nextTool` /
`mediationBrief` / `ideApplyBrief`.

## Every session

1. Enable MCP `uiforgemax`.
2. **`uiforgemax_preflight(project_root='…')`**
3. **`uiforgemax_start_run(mode='start', issue_key='…')`** or resume.
4. Tickets: **`uiforgemax_add_jira`** — never invent via `add_prompt`.
5. At plan gate: **STOP for the human** (`waitForHuman`). Enumerate every
   `filesToCreate` / `filesToModify` path. Do not auto-`approve_plan`
   (unless `UIFORGEMAX_SKIP_PLAN_APPROVAL=1`).
6. After approve: IDE-edit locked paths only → `uiforgemax_advance` once (verify /
   visual / test).

Follow `nextTool` except at IDE-apply pause (edit first). At `awaiting_mediation`,
use `mediationBrief` + `uiforgemax_submit_mediation` (intent JSON only for plans).

## Do not pause to ask — just drive the pipeline

The ONLY human stops are: (a) plan gate, (b) `stop: true` you cannot resolve,
(c) MCP unavailable, (d) genuine BLOCKED. Otherwise chain
`add_jira → advance → mediate → … → plan gate → (human) → IDE edit → advance`.

See `agent/uiforgemax-agent.md` for the full allowlist.
