# UiForgeMax — Driving Agent

**MCP-only.** Use only `uiforgemax_*` tools. If MCP is not connected, **stop** — do not use other tools.

## Every session

1. Enable the MCP `uiforgemax` server in your IDE's MCP settings.
2. Use **Agent** mode (any capable model) with this file as the agent rules.
3. First tool: **`uiforgemax_preflight(project_root='…')`** — verifies Python + Graphify + workspace; saves session.
4. Then **`uiforgemax_start_run(mode='start', issue_key='…')`** for a clean slate (pass
   `issue_key` when the user named a ticket), or **`mode='resume'`** /
   **`uiforgemax_resume_run`** to continue an existing run.
5. For tickets: **`uiforgemax_add_jira`** — never invent the issue via `add_prompt`.
6. At plan gate: **STOP and wait for the human** (`waitForHuman: true`). Display
   `planApproval` fully — topology, scope, ACs, risks, blockers, validation plan,
   and **enumerate every path in `filesToCreate` and `filesToModify` by name, one
   per line**. Never summarize the file list as "several files" or omit it, even
   if long — the human is approving those exact paths. Do not auto-`approve_plan`.
   Local/CI: `UIFORGEMAX_SKIP_PLAN_APPROVAL=1`.

Follow `nextTool` in each JSON response. Do not edit project files directly — MCP implements after the sole plan approval.

At **`awaiting_mediation`**, use your IDE model + `uiforgemax_submit_mediation`.
The response's `modelMediation.artifactContents` inlines the small artifacts — use
those directly, do **not** re-Read those files. Only Read a `readArtifacts` path
that is absent from `artifactContents` (large files like HTML / source snapshots).

## Do not pause to ask — just drive the pipeline

**Never stop to ask "should I continue / advance?"** Drive the pipeline forward on
your own. The ONLY places you hand control back to the human are:
(a) `waitForHuman: true` (the plan-approval gate), (b) `stop: true` in a response,
(c) MCP unavailable, or (d) a genuine `BLOCKED`/error you cannot resolve.

In every other state — including right after `add_jira`/`add_prompt`, after each
mediation `submit`, and between all automated stages — immediately call whatever
`nextTool` says (usually `uiforgemax_advance`) without asking permission first.
Chaining `add_jira → advance → (mediation) → submit → advance → …` up to the plan
gate is the expected, correct behavior. Do not narrate "if you want, I can
continue" — just continue.

---

See `agent/uiforgemax-agent.md` for the full tool allowlist and detailed workflow.
UiForgeMax is IDE-agnostic — load these rules via whatever mechanism your IDE uses
for persistent agent instructions.
