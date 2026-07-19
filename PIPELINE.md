# UiForgeMax Pipeline Reference

> Runtime guide for MCP-driven flow. **Not loaded by the agent** — call `uiforgemax_get_pipeline_guide` for live reference + run stats.

## Control model

- **MCP** drives every stage (zero LLM API keys inside UiForgeMax)
- **Graphify** indexes + queries the project graph (token saver)
- **IDE agent** (Cursor/VS Code): tool allowlist only + human conversation at gates

Artifacts: `%APPDATA%\UiForgeMax\runs\<run-id>\`

---

## Flow diagram

```text
start_run → add_* inputs → advance
  → intake → arch_detect → normalize → graphify_update
  → graph_query_plan → graph_query_exec → requirement_map
  → api_resolve → [Gate 1?]
  → understanding → Gate 2 → plan → plan_review → Gate 3
  → implement → test → handover
```

---

## Stages (18 steps)

| # | Stage ID | Tool | LLM keys | Output artifacts |
|---|----------|------|----------|------------------|
| 0 | `0_intake` | `advance` | No | validates inputs registered |
| 0.5 | `0.5_arch_detect` | `advance` | No | `run.json` architecture |
| 1 | `1_image_convert` | `advance` | No | (feeds normalize) |
| 2 | `2_normalize` | `advance` | No | `requirements.normalized.json`, `visual-spec.json` |
| 3 | `3_graphify_update` | `advance` | No | `graph/index.json` |
| 4 | `4_graph_query_plan` | `advance` | No | `graph/queries.json` |
| 4.5 | `4.5_graph_query_exec` | `advance` | No | `graph/query-results.json` |
| 4.6 | `4.6_requirement_map` | `advance` | No | `graph/requirement-map.json`, `graph/context-pack.json` |
| 5 | `5_api_resolve` | `advance` | No | `api-resolution.json` |
| 5.5 | `5.5_gate_api` | `approve_api` | No | conditional human gate |
| 6 | `6_understanding` | `advance` | No | `plans/understanding.md` |
| 6.5 | `6.5_gate_understanding` | auto | No | auto-pass (folded into plan approval) |
| 7 | `7_plan` | `advance` | No | `plans/implementation-plan.json` |
| 8 | `8_plan_review` | `advance` | No | `plans/plan-review.json` |
| 8.5 | `8.5_gate_plan` | `approve_plan` | No | **sole** human gate before implement |
| 9 | `9_implement` | `advance` | No | locked `approved-plan.json` → workspace files |
| 10 | `10_test` | `advance` + TEST_GENERATION | No | unit tests + coverage → `tests/unit-results.json` |
| 11 | `11_handover` | `advance` | No | `handover/delivery-report.md` |

Per-run timing: `stats.json` in the run folder.

---

## MCP tools

| Tool | Purpose |
|------|---------|
| `uiforgemax_start_run` | Create session; set `project_root` |
| `uiforgemax_add_jira` | Fetch Jira (REST/fixture) |
| `uiforgemax_add_image` | Copy wireframe |
| `uiforgemax_add_html` | Save HTML input |
| `uiforgemax_add_prompt` | Save text requirement |
| `uiforgemax_advance` | Run automatic stages until gate/done |
| `uiforgemax_approve_api` | Conditional API gate |
| `uiforgemax_approve_understanding` | Legacy / no-op (auto-pass) |
| `uiforgemax_approve_plan` | Sole human gate — locks plan |
| `uiforgemax_request_changes` | Delta revise at plan (or normalize) |
| `uiforgemax_answer_clarifications` | Answer graph clarification questions |
| `uiforgemax_get_run_status` | Full run state |
| `uiforgemax_get_pipeline_guide` | This guide + optional stats |
| `uiforgemax_list_runs` | List sessions |
| `uiforgemax_cancel_run` | Cancel |

Every tool response includes JSON: `nextTool`, `status`, `blockedTools`.

---

## Graphify query types (no LLM)

| Query type | Question asked of the graph |
|------------|----------------------------|
| `components.match` | Which UI components match required keywords? |
| `apis.match` | Which endpoints exist vs missing for data operations? |
| `clients.match` | Which client methods exist vs missing? |
| `pages.in_app` | Existing pages in target app? |
| `navigation.sidebar` | Where is navigation configured? |
| `projects.in_domain` | NX projects in target domain? |
| `tests.pattern` | Test file conventions in app? |
| `acceptance.map` | Map AC items to strategies |

Results merge into **`requirement-map.json`** → plan → implement.
