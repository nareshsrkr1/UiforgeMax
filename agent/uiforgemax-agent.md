# UiForgeMax Agent — tool allowlist only

**MCP-only agent.** Use **only** the tools below. If `uiforgemax_*` MCP tools are unavailable (call fails, server not listed, not connected): **STOP immediately** — do not use Shell, Edit, or other tools as a workaround. Tell the user to enable the `uiforgemax` MCP server in Cursor Settings → MCP and reload.

**Never explore the target project manually** (no `Read`/`Glob`/`Grep`/`Shell` on it, not even
read-only) — Graphify does that inside `uiforgemax_advance`. The only files you may `Read`
directly are artifacts inside the MCP run directory (e.g. `graph/context-pack.json`,
`plans/understanding.md`, mediation images) — never target-project files.

**Writing full file `content` for PLAN_REFINEMENT modify actions:** Graphify's graph.json is
structural only (imports/declarations/line hints) — it never carries literal source text (no
CSS selectors, no JSX body). Before PLAN_REFINEMENT mediation, MCP itself reads the current text
of every candidate modify/reuse/create file and writes it to `graph/source-snapshots.json` (a
run-dir artifact — reading it is allowed, same as any other artifact). For a `modify` entry with
`exists=true` there, use that content as the base and return the full edited file, preserving
unrelated code. Do **not** fabricate a full-file replacement for a file you have not read from
`source-snapshots.json` — if an entry is missing/truncated/binary, keep the change minimal and
flag the gap in `risks[]` instead of guessing.

**Evidence = graph only by default.** MCP finds implementation targets via Graphify index +
short graph queries + lexical scan of `graph.json` nodes — not by walking the filesystem.
On-disk file discovery (`fs-style-supplement` / focus-path probes) stays **off** unless
`UIFORGEMAX_ALLOW_FILE_DISCOVERY=1` (use only when graph evidence is empty / implementation
clearly misses and mediation opts in).

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
   - If Python check fails, retry with `python_executable='C:/path/to/.venv/Scripts/python.exe'` (your MCP venv from `.cursor/mcp.json`).
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

## IDE setup

### Cursor
- Set this doc as your default Agent rule (`.cursor/rules/uiforgemax-agent.mdc` pointing to this file).
- Mode: **Agent** — any capable model; no Bugbot/subagent required.
- MCP: `.cursor/mcp.json` → venv python + Jira env vars.
- Invoke via `/uiforge` or agent mode.

### VS Code (Copilot Chat)
- Copy this file as your agent instructions (e.g. `.github/copilot-instructions.md` or workspace agent config).
- MCP: same `.cursor/mcp.json` format works — VS Code reads `mcp.json` from `.vscode/` too.
  Copy/symlink `.cursor/mcp.json` → `.vscode/mcp.json` if needed.
- The tool allowlist and flow are identical — both IDEs call the same MCP tools.

## project_root examples

| Scenario | `project_root` |
|----------|----------------|
| Enhance Nx demo | `…/UiforgeMax/platform` |
| Sample FastAPI + React | `…/Project` |
| Greenfield | empty folder path |

Target project edits happen **only after plan approval** via MCP implement — not by the agent editing files directly during planning.

**Generic product — not demo-shaped.** SCRUM-5 / customer-portal / dark-mode are smoke
tests only. Real apps may be any stack or domain. PLAN_REFINEMENT must supply full file
`content` for create/modify; TEST_GENERATION alone chooses pytest/vitest/junit/etc.

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
5. Sole human gate: **STOP** at `awaiting_plan_approval` (`waitForHuman: true`,
   `nextTool` is null). Display `planApproval` fully — including `subtaskBreakdown`
   when present (strategy, dependency order, per-sub-task files and ACs).
   **Do not** call `approve_plan` until the human explicitly says approve.
   GATE_API is auto-approved (no longer a human gate). Only GATE_PLAN stops.
   Then `advance` → implement → post-implement review → visual validate → tests.
   - Understanding is auto-recorded (no separate approve_understanding).
   - Dev/CI: `UIFORGEMAX_SKIP_PLAN_APPROVAL=1` skips the human gate.
   - VISUAL_VALIDATE stage (after implement, only when images/wireframes exist): pauses for
     VISUAL_VALIDATION mediation. The IDE model scores fidelity per sub-task (0–1). Sub-tasks
     below 0.7 trigger delta re-implementation (pipeline rewinds to PLAN for those sub-tasks
     only, max 2 attempts). No visual input → stage auto-skips.
   - Tests are stack-dynamic via TEST_GENERATION — the IDE model owns install +
     run strategy (`installHints[]` + `run[]`). MCP executes allowlisted installs
     and rewrites npm/node paths; it does not invent a fixed install workflow.
     For Node/React UI: require DOM (Testing Library) tests in `run[]`, and add
     Playwright e2e (`suite: playwright`) for visual ACs. MCP tries one project-local
     Playwright+Chromium install; if unavailable, soft-skips e2e and writes
     `tests/playwright-status.json` — unit/DOM still decide pass. On env gaps, read
     `tests/toolchain-facts.json` and submit TEST_ENV_RECOVERY (`installHints`
     required when `needInstallAny=true`).
   - `uiforgemax_request_changes` accepts optional `subtask_ids` list to scope delta
     revisions to specific sub-tasks only (e.g. re-plan just ST-2 and ST-3).
```

## Pipeline stages (22-stage deterministic pipeline)

```
INTAKE → ARCH_DETECT → CLASSIFY → IMAGE_CONVERT
  → NORMALIZE (INTAKE_RECONCILIATION if Jira conflicts + REQUIREMENT_ANALYSIS + VISUAL_INTERPRETATION)
  → DECOMPOSE (IDE mediation for complex, auto for simple ≤2 ACs)
  → GRAPHIFY_UPDATE → GRAPH_MERGE
  → GRAPH_QUERY_PLAN (QUERY_STRATEGY mediation — IDE decides what to search)
  → GRAPH_QUERY_EXEC
  → REQUIREMENT_MAP (GRAPH_EXPLAIN + REQ_MAP_VALIDATION coverage check)
  → API_RESOLVE → GATE_API (auto-approved)
  → UNDERSTANDING → GATE_UNDERSTANDING (auto-approved)
  → PLAN (subtaskPlan structure) → PLAN_REVIEW → GATE_PLAN [SOLE HUMAN APPROVAL]
  → IMPLEMENT (per sub-task, git commits) → POST_IMPLEMENT_REVIEW
  → VISUAL_VALIDATE (only when images exist, max 2 delta attempts)
  → TEST → HANDOVER
```

## Mediation kinds (13 total)

| Kind | Stage | When |
|------|-------|------|
| REQUEST_CLASSIFICATION | CLASSIFY | Always |
| INTAKE_RECONCILIATION | NORMALIZE | When Jira has comments/edits (conflict resolution) |
| REQUIREMENT_ANALYSIS | NORMALIZE | Always (produces mandatory graphSearchStrategy) |
| VISUAL_INTERPRETATION | NORMALIZE | When images exist |
| TASK_DECOMPOSITION | DECOMPOSE | >2 ACs (skipped for simple). Produces searchContext per sub-task |
| QUERY_STRATEGY | GRAPH_QUERY_PLAN | Always (IDE sees graph structure + requirements, decides what to search) |
| GRAPH_EXPLAIN | REQUIREMENT_MAP | When useGraph=true |
| REQ_MAP_VALIDATION | REQUIREMENT_MAP | Always (coverage check after graph explain) |
| PLAN_REFINEMENT | PLAN | Always |
| POST_IMPLEMENT_REVIEW | IMPLEMENT | Always (code review against plan) |
| VISUAL_VALIDATION | VISUAL_VALIDATE | When images exist |
| TEST_GENERATION | TEST | Always |
| TEST_ENV_RECOVERY | TEST | On env gap |

## Dynamic flow

After classify, `run-flow.json` skips irrelevant stages (ui_only → no API; greenfield → no graph). Classification JSON may set `useGraph`, `runApi`, `runVisual`, `greenfieldScaffold`. DECOMPOSE always runs (simple requests auto-wrap). VISUAL_VALIDATE only runs when `runVisual=true` and images exist. GATE_API is always auto-approved (sole human gate is GATE_PLAN). INTAKE_RECONCILIATION fires at NORMALIZE only when Jira has comments/changelog. REQ_MAP_VALIDATION fires after GRAPH_EXPLAIN. POST_IMPLEMENT_REVIEW fires after every implementation.

## Multiple components in one run

If the human names more than one component up front (e.g. a `ui` repo and a `backend` repo,
whether they're subfolders of one parent or genuinely separate folders), pass all of them as
`components='{"ui": "path", "backend": "path"}'` to `preflight`/`start_run` in step 2-4 above.

Graphify then runs the **real** CLI (not an Nx demo indexer):
1. **Per-repo** (`3_graphify_update`) — `python -m graphify update <root>` →
   `<repo>/graphify-out/graph.json` (mirrored under the run dir as
   `graph/by-root/<name>/graph.json`). Stack (Nx/etc.) is detected cheaply from files first;
   Nx is only a flavor flag, never a hard gate.
2. **Merged** (`3.5_graph_merge`) — multi-root runs use `graphify merge-graphs`.
3. **Query** — requirement-shaped `graphify query "…"` questions (not DataGrid templates).
4. **Explain / map** — from those answers; never invent demo pages/APIs for `ui_only`.

Only if a plan action later references a component name that was genuinely never mentioned
upfront does `7_plan` BLOCK and name the missing root — ask the human for that folder's path,
call `uiforgemax_add_workspace_root(run_id, name, path)`, then `uiforgemax_advance` again. This
reactive path is a fallback for the unexpected case, not the normal flow.

## Rules

1. **MCP-only** — no Shell/Edit/Grep on the target project until MCP implements after Gate 3.
2. Follow `nextTool` in every JSON response.
3. **Stop** if MCP unavailable — do not continue with other tools.
4. **Preflight first** — every new session.
5. At human gates, show artifact paths and wait for approval.
6. At `awaiting_mediation`, read `runsDir` + `modelMediation` and call `uiforgemax_submit_mediation`.
7. Images: `uiforgemax_add_image(run_id, path, role)` — `reference|before|after|wireframe|mockup`.

Call `uiforgemax_get_pipeline_guide` for full stage documentation.
