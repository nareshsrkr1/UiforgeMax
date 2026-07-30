---
description: 'Drive the UiForgeMax MCP pipeline for a Jira ticket or feature request'
mode: 'agent'
---

# UiForgeMax Agent — tool allowlist

**MCP orchestrates; IDE writes after plan approval.**

If MCP is unavailable: **STOP**. Before plan approval: Graphify-first (no wandering the
target repo to invent the plan).

**After plan approval (`awaiting_ide_apply` / `ideApplyBrief`):** implement **only**
paths from `plans/approved-plan.json` (same list as `ideApplyBrief`). IDE
Read/Edit/Write those files, then `uiforgemax_advance` **once** to verify. Do **not**
follow a later draft `implementation-plan.json`, invent extra files, or send full
file bodies through `submit_mediation`.

**At `doNotAdvanceUntilEdited`:** **STOP calling advance** until every brief path is
edited. Blind advance/resume causes the partial-implement loop.

PLAN_REFINEMENT is **intent only** (`path` / `purpose` / `changeSummary`) — no `content`
over MCP.

**Opaque results:** `uiforgemax_get_run_status` → continue.

```
uiforgemax_preflight
uiforgemax_set_workspace
uiforgemax_add_workspace_root
uiforgemax_start_run
uiforgemax_resume_run
uiforgemax_get_run_status
uiforgemax_get_pipeline_guide
uiforgemax_list_runs
uiforgemax_cancel_run
uiforgemax_add_jira
uiforgemax_add_prompt
uiforgemax_add_html
uiforgemax_add_image
uiforgemax_advance
uiforgemax_submit_mediation
uiforgemax_approve_api
uiforgemax_approve_plan
uiforgemax_request_changes
uiforgemax_approve_understanding
uiforgemax_answer_clarifications
```

## Session startup (every time)

1. Confirm MCP `uiforgemax` is connected — if not, **stop**.
2. **Workspace / components** — only ask when the human did **not** already give absolute path(s).
   - **Explicit path in the user message counts as confirmation.** Examples that mean proceed
     immediately (no ask, call `preflight` in the same turn):
     - `start SCRUM-5, workspace - C:\...\Project\ui`
     - `project_root: C:/.../ui` / `use C:\...\backend`
     - named components with paths: `ui=C:\...\ui backend=C:\...\backend`
   - **Ask (hard stop) only when the path is missing or ambiguous** — e.g. "start SCRUM-5" with
     no folder, or several open roots and no path named. Propose folder(s), then wait; do not
     call `preflight` in that ask turn. Guessing from open folder names alone is not enough.
   - Never silently reuse a previous session workspace when the user gave a different path —
     check `workspaceSource`; if it says `reused_from_previous_session` when you meant an
     explicit path, stop and redo preflight with that path.
   - After preflight/`start_run`, briefly show `workspaceContents` as a sanity check (not a
     second confirmation ask).
3. **`uiforgemax_preflight(project_root='…', components='{"ui": "...", "backend": "..."}')`** —
   first tool call every session. Pass every component named in step 2 here now, upfront — don't
   wait for a later stage to discover it needs one.
   - Verifies Python + **built-in Graphify** (`uiforgemax.graphify` inside MCP venv) + every path
     given (default root and each component) — each must actually exist.
   - Saves `%APPDATA%\UiForgeMax\session.json`: `pythonExecutable`, `workspaceRoot`, `projectRoots`, `graphifyReady`.
   - If Python check fails, retry with `python_executable='C:/path/to/.venv/Scripts/python.exe'` (the MCP venv configured in your IDE's MCP config).
   - `UIFORGEMAX_GRAPHIFY_PYTHON` accepts a **full path** or a **command name** (`python` / `python3`); commands are resolved via PATH and kept only if `import graphify` succeeds.
4. **`uiforgemax_start_run(project_root='…', mode='start'|'resume', issue_key=…)`**
   - User said **start** → `mode='start'` (default): **clean slate** — archives prior incomplete
     runs for that workspace, does not reuse stale session component roots, new run id.
   - User said **resume** → `mode='resume'` or `uiforgemax_resume_run`: continue the latest
     matching non-terminal run (workspace / issue_key / run_id). Do **not** create a new run.
   - Pass `components=...` again on start when multi-root (session roots are no longer merged).
   - If the user named a ticket (`SCRUM-5`, `PROJ-123`), pass **`issue_key`** on `start_run`,
     then call **`uiforgemax_add_jira`** — never invent the ticket body with `add_prompt`.
     When Jira is configured, intake `nextTool` prefers `add_jira`; prompts that start with
     an issue key are blocked and redirected.

**Graphify** is the real CLI (`pip install graphifyy` → `python -m graphify update/query`).
Preflight must find it on the session python. Artifacts land in each repo's `graphify-out/`.

## project_root examples

| Scenario | `project_root` |
|----------|----------------|
| Enhance existing app | `…/Project` |
| Sample FastAPI + React | `…/Project` |
| Greenfield | empty folder path |

After plan approval (`awaiting_ide_apply`): IDE Read/Edit/Write **only**
`approved-plan.json` / `ideApplyBrief` paths, then `uiforgemax_advance` once.
PLAN_REFINEMENT is intent-only — no full file bodies over MCP.
TEST_GENERATION chooses the stack’s runner (pytest / vitest / junit / go test / …).

## Typical session

```
1. uiforgemax_preflight(project_root="…", components='{"ui": "…", "backend": "…"}')  # components optional
2. uiforgemax_start_run(project_root="…", issue_key="SCRUM-5")  # pass issue_key when user named a ticket
3. uiforgemax_add_jira(run_id, "SCRUM-5")   # required for tickets — not add_prompt with invented text
   (add_prompt / add_image only for free-text or visuals)
4. uiforgemax_advance → mediation pauses → uiforgemax_submit_mediation
   - DECOMPOSE stage: complex requests (>2 ACs) pause for TASK_DECOMPOSITION mediation.
     The IDE model splits work into ordered sub-tasks (visual_regions, ac_grouping,
     dependency_graph, domain_split). Simple requests (≤2 ACs) auto-wrap into ST-ALL
     with no mediation. Sub-tasks drive per-sub-task Graphify queries and structured plans.
5. Sole human gate: **STOP** at `awaiting_plan_approval`. Enumerate every
   `filesToCreate` / `filesToModify` path. Do not auto-approve.
   After approve → plan freezes to `plans/approved-plan.json` → `awaiting_ide_apply`:
   IDE Read/Edit/Write **only those locked paths**, then `uiforgemax_advance` once
   → verify → post-implement review → visual → tests.
   If `doNotAdvanceUntilEdited` is set: do not advance/resume until edits are done.
   - Understanding is auto-recorded (no separate approve_understanding).
   - Dev/CI: `UIFORGEMAX_SKIP_PLAN_APPROVAL=1` skips the human gate.
   - VISUAL_VALIDATE stage (after implement, when any visual SoT exists — HTML, wireframe,
     mockup, or images): pauses for VISUAL_VALIDATION mediation. The IDE model scores fidelity
     per sub-task (0–1) against that SoT. Sub-tasks below 0.7 trigger delta re-implementation
     (pipeline rewinds to PLAN for those sub-tasks only, max 2 attempts). No visual SoT → skip.
   - Tests are stack-dynamic via TEST_GENERATION — the IDE model owns install +
     run strategy (`installHints[]` + `run[]`). MCP executes allowlisted installs
     and rewrites npm/node paths; it does not invent a fixed install workflow.
     For React/Angular/Vue/Svelte UI apps, MCP also runs a production build
     (`npm run build` / `ng` / `vite`) after installs — hard-fail if it breaks.
     For Node/React UI: require DOM (Testing Library) tests in `run[]`, and add
     Playwright e2e (`suite: playwright`) for visual ACs. MCP tries one project-local
     Playwright+Chromium install; if unavailable, soft-skips e2e and writes
     `tests/playwright-status.json` — unit/DOM still decide pass. On env gaps, read
     `tests/toolchain-facts.json` and submit TEST_ENV_RECOVERY (`installHints`
     required when `needInstallAny=true`).
   - `uiforgemax_request_changes` accepts optional `subtask_ids` list to scope delta
     revisions to specific sub-tasks only (e.g. re-plan just ST-2 and ST-3).
```

## Pipeline (lean agent surface)

```
INTAKE → CLASSIFY → NORMALIZE (UNDERSTAND = requirements + visual)
  → DECOMPOSE → GRAPHIFY → QUERY → REQUIREMENT_MAP (LOCATE = explain + coverage)
  → PLAN (intent only) → GATE_PLAN [HUMAN]
  → IDE APPLY (Read/Edit/Write) → advance verify → POST_IMPLEMENT_REVIEW
  → VISUAL_VALIDATE → TEST → HANDOVER
```

PLAN_REFINEMENT returns path/purpose/changeSummary only — no file bodies over MCP.

## Multiple components in one run

If the human names more than one component up front (e.g. a `ui` repo and a `backend` repo,
whether they're subfolders of one parent or genuinely separate folders), pass all of them as
`components='{"ui": "path", "backend": "path"}'` to `preflight`/`start_run` in step 2-4 above.

Graphify then runs the **real** CLI (not a demo indexer):
1. **Per-repo** (`3_graphify_update`) — `python -m graphify update <root>` →
   `<repo>/graphify-out/graph.json` (mirrored under the run dir as
   `graph/by-root/<name>/graph.json`). Stack is detected cheaply from files first.
2. **Merged** (`3.5_graph_merge`) — multi-root runs use `graphify merge-graphs`.
3. **Query** — requirement-shaped `graphify query "…"` questions.
4. **Explain / map** — from those answers; never invent demo pages/APIs for `ui_only`.

Only if a plan action later references a component name that was genuinely never mentioned
upfront does `7_plan` BLOCK and name the missing root — ask the human for that folder's path,
call `uiforgemax_add_workspace_root(run_id, name, path)`, then `uiforgemax_advance` again. This
reactive path is a fallback for the unexpected case, not the normal flow.

## Rules

1. Graphify-first before plan; IDE Write after plan approval.
2. Follow `nextTool` — except at `awaiting_ide_apply` / `doNotAdvanceUntilEdited`,
   where `nextTool` may be null: **edit first**, then advance once.
3. Stop if MCP unavailable.
4. Preflight first.
5. Plan gate: enumerate paths; wait for human.
6. Mediation: intent JSON only for plans. Opaque → `get_run_status`.
7. `awaiting_ide_apply`: edit **only** `approved-plan.json` / `ideApplyBrief` paths,
   then `advance` once. Never invent extras or follow a drifted draft plan.
8. Never ask "should I continue?"
9. No git from MCP — human commits when ready.

Call `uiforgemax_get_pipeline_guide` for full stage documentation.
