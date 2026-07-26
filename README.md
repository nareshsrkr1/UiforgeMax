# UiForgeMax MCP (Python)

Stage-gated MCP server: multi-form intake → **classify** (dynamic flow) → normalize → Graphify → plan → approval → implement → test → handover.

**MCP drives the flow** (state machine + `nextTool` in every response). The Cursor agent uses **only** the `uiforgemax_*` tool allowlist — see `agent/uiforgemax-agent.md`.

## Architecture in one line

MCP (Python, deterministic: state machine, Graphify index/query, plan scaffolding, file writes, tests) + **your IDE model** (judgment at mediation pauses: classification, requirement analysis, visual interpretation, graph explanation, plan refinement, test generation) + **human** (approves at Gates 1–3 before any workspace writes).

## Dynamic flow

After the `classify` stage, the pipeline writes `run-flow.json` deciding which stages actually run:

| Classification | Graph intelligence | API stages | Visual stages |
|-----------------|--------------------|------------|----------------|
| `ui_only` | runs | **skipped** | runs if images |
| `api_only` | runs | runs | skipped |
| `full_stack` | runs | runs | runs if images |
| `greenfield` / empty repo | **skipped** (scaffold instead) | runs (new API) | optional |

Your IDE model's `REQUEST_CLASSIFICATION` mediation response can set `useGraph`, `runApi`, `runVisual`, `greenfieldScaffold` explicitly.

## Temp / run artifacts

All run data is stored under **system app data**, not in the project repo:

| OS | Default path |
|----|----------------|
| Windows | `%APPDATA%\UiForgeMax\runs\<run-id>\` |
| Other | `$TMP/UiForgeMax/runs/<run-id>/` |

Graph index cache: `%APPDATA%\UiForgeMax\graph\<hash>\`
Session (python path + workspace, saved by preflight): `%APPDATA%\UiForgeMax\session.json`

Override runs root with env `UIFORGEMAX_RUNS_ROOT`.  
`UIFORGEMAX_RUNS_RETENTION_DAYS` defaults to **14** — on each `start_run`, async
best-effort prune of run dirs (and `_archive/`) older than N days; logs to
`runs/_prune.log` (never blocks or fails the pipeline). Set `0` to disable.

## Configure Cursor

Copy `mcp.json.example` → `.cursor/mcp.json` and set:

```json
"command": "C:/path/to/UiforgeMax/.venv/Scripts/python.exe",
"env": {
  "JIRA_BASE_URL": "https://your-org.atlassian.net",
  "JIRA_EMAIL": "${env:JIRA_EMAIL}",
  "JIRA_API_TOKEN": "${env:JIRA_API_TOKEN}"
}
```

`JIRA_EMAIL` is **optional**: set it for Atlassian Cloud (Basic auth, email+token); omit it for Jira Server/Data Center (Bearer PAT, token only).

Local dev without Jira: skip `add_jira` and use `add_prompt` / `add_image` / `add_html` instead.

## Run (standalone, outside Cursor)

```powershell
pip install -e ".[dev]"
$env:PYTHONPATH="src"
python -m uiforgemax.server
```

## Tool flow

1. `uiforgemax_preflight(project_root="C:/path/to/workspace")` — verify Python + Graphify + workspace; saves session
2. `uiforgemax_start_run()` — uses session workspace, or pass `project_root` explicitly
3. `uiforgemax_add_jira` / `add_prompt` / `add_image` / `add_html`
4. `uiforgemax_advance` → **first mediation pause: REQUEST_CLASSIFICATION** → `uiforgemax_submit_mediation`
5. `uiforgemax_advance` → more mediation pauses as needed → stops at **plan approval** (sole human gate; understanding is auto-recorded)
6. `uiforgemax_approve_plan` → freezes `plans/approved-plan.json` → `advance` → implement that exact plan + unit tests/coverage + handover

Every response is JSON with `nextTool`, `status`, `blockedTools`. API approval only appears when the flow includes API stages and a gap requires approval — skipped entirely for `ui_only` requests and greenfield scaffolds.

See `agent/uiforgemax-agent.md` for the full driving-agent workflow and rules — this file is
pulled into every Cursor session automatically via `.cursor/rules/uiforgemax-agent.mdc`.

## Tests

```powershell
$env:PYTHONPATH="src"
python -m pytest -q
```

`tests/test_flow.py` covers the happy path, gates, mediation, and classification.
`tests/test_flow_router.py` covers dynamic branching (ui_only, api_only, greenfield scaffold end-to-end).
`tests/test_graphify.py` covers the deterministic graph indexer/query engine.

Integration tests copy `platform/` (a sample Nx-style demo) to a temp workspace and run the pipeline end-to-end — no repo files are mutated.
