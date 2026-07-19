"""Pipeline stage and tool reference — MCP runtime guide (not MASTER_PLAN)."""

from __future__ import annotations

import json

PIPELINE_GUIDE = {
        "architecture": {
        "mcpDrives": True,
        "llmKeysRequired": False,
        "agentRole": "Tool allowlist only; follow nextTool in JSON responses",
        "graphifyRole": "Deterministic index + query + requirement-map (skipped for greenfield / empty repo)",
        "flowRouter": "run-flow.json — dynamic activeStages from classification (ui_only skips API, greenfield skips graph)",
        "ideAgentRole": "Mediate judgment at model_mediation pauses; present human gates",
        "modelMediation": {
            "tool": "uiforgemax_submit_mediation",
            "skipEnv": "UIFORGEMAX_SKIP_MEDIATION=1",
            "kinds": [
                "REQUEST_CLASSIFICATION @ 0.6_classify",
                "REQUIREMENT_ANALYSIS @ 2_normalize",
                "VISUAL_INTERPRETATION @ 2_normalize (when image input)",
                "GRAPH_EXPLAIN @ 4.6_requirement_map",
                "PLAN_REFINEMENT @ 7_plan",
                "TEST_GENERATION @ 10_test",
                "TEST_ENV_RECOVERY @ 10_test (when tool/env gap)",
            ],
        },
    },
    "artifactsRoot": "%APPDATA%/UiForgeMax/runs/<run-id>/",
    "stages": [
        {
            "id": "0_intake",
            "tool": "uiforgemax_add_* + advance",
            "llm": False,
            "description": "Register inputs (Jira/image/HTML/prompt). Jira fetched via REST PAT. Files saved under inputs/.",
            "outputs": ["inputs/jira.raw.json", "inputs/*.png", "inputs/prompt.txt"],
        },
        {
            "id": "0.5_arch_detect",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Validate project_root exists. Detect nx-monorepo vs standard layout.",
            "outputs": ["run.json architecture field"],
        },
        {
            "id": "0.6_classify",
            "tool": "uiforgemax_advance → uiforgemax_submit_mediation",
            "llm": "IDE model (REQUEST_CLASSIFICATION)",
            "description": (
                "Gather deterministic intake signals (input modes, image roles incl. before/after, "
                "Jira/prompt keywords, repo state) and write a heuristic default. Pause for the IDE "
                "model to decide the request type dynamically (greenfield, UI-only, API, full-stack, "
                "before/after redesign, OpenFin/desktop, migration, refactor, bugfix, …). The decision "
                "sets policy and drives the rest of the pipeline."
            ),
            "outputs": ["classification-signals.json", "request-classification.json", "run-flow.json"],
        },
        {
            "id": "1_image_convert",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Convert wireframe metadata to visual-spec.json using rules (no vision API).",
            "outputs": ["visual-spec.json (via normalize)"],
        },
        {
            "id": "2_normalize",
            "tool": "uiforgemax_advance → uiforgemax_submit_mediation",
            "llm": "IDE model (mediation)",
            "description": "Merge Jira AC, comments/overrides, prompt, visual-spec. Pause for IDE REQUIREMENT_ANALYSIS (+ VISUAL if image).",
            "outputs": ["requirements.normalized.json", "visual-spec.json", "mediation/*.json"],
        },
        {
            "id": "3_graphify_update",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": (
                "Real Graphify update per registered root: `python -m graphify update <root>` "
                "(SKIPPED when greenfield / empty — see run-flow.json). Writes "
                "<repo>/graphify-out/graph.json. Cheap stack detect (nx.json etc.) runs first; "
                "Nx is a flavor flag only — never a hard gate."
            ),
            "outputs": ["<repo>/graphify-out/graph.json", "graph/by-root/<name>/graph.json"],
        },
        {
            "id": "3.5_graph_merge",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": (
                "When multiple roots are registered, runs `graphify merge-graphs` into "
                "graph/merged-graph.json. Single-root runs copy that root's graph pointer."
            ),
            "outputs": ["graph/merged.json", "graph/index.json", "graph/merged-graph.json"],
        },
        {
            "id": "4_graph_query_plan",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Plan graph queries from requirements (components, APIs, pages, nav, tests, AC).",
            "outputs": ["graph/queries.json"],
        },
        {
            "id": "4.5_graph_query_exec",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Execute each query against index. Answers saved with explanations.",
            "outputs": ["graph/query-results.json"],
        },
        {
            "id": "4.6_requirement_map",
            "tool": "uiforgemax_advance → uiforgemax_submit_mediation",
            "llm": "IDE model (GRAPH_EXPLAIN)",
            "description": "Merge query answers + requirements → requirement-map. IDE explains graph hits and adjusts map.",
            "outputs": ["graph/requirement-map.json", "graph/context-pack.json", "graph/mediation-explain.json"],
        },
        {
            "id": "5_api_resolve",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Resolve API policy (SKIPPED for ui_only). Greenfield uses scaffold api-resolution.json.",
            "outputs": ["api-resolution.json"],
        },
        {
            "id": "5.5_gate_api",
            "tool": "uiforgemax_approve_api | request_changes",
            "llm": False,
            "description": "HUMAN GATE (conditional). Approve API strategy when backends missing in full_stack mode.",
            "outputs": ["approvals.api"],
        },
        {
            "id": "6_understanding",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Generate understanding.md from requirement-map + api-resolution for human review.",
            "outputs": ["plans/understanding.md"],
        },
        {
            "id": "6.5_gate_understanding",
            "tool": "auto (no human pause)",
            "llm": False,
            "description": "Auto-passes. Understanding artifacts are folded into the sole plan approval package.",
            "outputs": ["plans/understanding-approval.json", "approvals.understanding (system-auto)"],
        },
        {
            "id": "7_plan",
            "tool": "uiforgemax_advance → uiforgemax_submit_mediation",
            "llm": "IDE model (PLAN_REFINEMENT)",
            "description": (
                "Build implementation-plan from requirement-map; IDE refines with visual compliance. "
                "Before mediation, MCP itself (not the driving agent) reads the literal CURRENT "
                "content of every modify/reuse/create candidate file into graph/source-snapshots.json "
                "— Graphify's graph.json is structural only (no CSS selectors/JSX body/business logic), "
                "so this is the only safe way for the IDE model to write a correct full-file `content` "
                "for modify actions without guessing and silently deleting unrelated code. "
                "A create/modify action may set \"root\" to a name other than the default project_root "
                "for multi-repo work (e.g. separate ui/api repos not sharing a parent folder). If that "
                "name isn't registered yet, this stage BLOCKS and asks for uiforgemax_add_workspace_root "
                "— it never assumes or guesses a path."
            ),
            "outputs": [
                "plans/implementation-plan.json",
                "plans/implementation-plan.md",
                "graph/source-snapshots.json",
            ],
        },
        {
            "id": "8_plan_review",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": "Validate plan covers all AC mappings and file actions. Writes plan-review.json.",
            "outputs": ["plans/plan-review.json"],
        },
        {
            "id": "8.5_gate_plan",
            "tool": "uiforgemax_approve_plan | request_changes",
            "llm": False,
            "description": (
                "SOLE HUMAN GATE — waitForHuman=true; do not auto-approve. "
                "Show planApproval, wait for the human, then approve_plan. "
                "Dev skip: UIFORGEMAX_SKIP_PLAN_APPROVAL=1."
            ),
            "outputs": ["approvals.plan", "plans/approved-plan.json"],
        },
        {
            "id": "9_implement",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": (
                "Apply the locked approved-plan.json (same plan the human approved). "
                "No re-planning. Git branch + per-file commits."
            ),
            "outputs": ["implementation/diff-summary.json", "workspace files"],
        },
        {
            "id": "10_test",
            "tool": "uiforgemax_advance → uiforgemax_submit_mediation",
            "llm": "IDE model (TEST_GENERATION)",
            "description": (
                "IDE TEST_GENERATION (stack-dynamic): model chooses framework/paths/commands "
                "(Java/Maven, pytest, vitest, go test, …). MCP writes mediated test files and "
                "runs mediated run[] commands. If a tool is missing, pauses for "
                "TEST_ENV_RECOVERY (alternate run[] / allowlisted installHints / skipTests), "
                "then retries once."
            ),
            "outputs": [
                "tests/generated-tests.json",
                "tests/unit-results.json",
                "tests/env-gap.json",
                "tests/recovery-log.json",
            ],
        },
        {
            "id": "11_handover",
            "tool": "uiforgemax_advance",
            "llm": False,
            "description": (
                "Write delivery report with branch, files, test results. "
                "If install times out (~15 min default), also writes handover/install-paused.md "
                "and status awaiting_user_install — resume/advance continues tests only."
            ),
            "outputs": ["handover/delivery-report.md", "handover/install-paused.md"],
        },
    ],
    "tools": {
        "uiforgemax_preflight": (
            "First call each session — verify Python, Graphify, workspace(s); save session.json. "
            "Ask the human up front which components/repos this involves (e.g. ui + backend) and "
            "pass them all as `components='{\"ui\": \"...\", \"backend\": \"...\"}'` here (or to "
            "start_run) — each gets its own Graphify artifacts, not just the default project_root."
        ),
        "uiforgemax_set_workspace": "Set project_root for session (and optional run_id)",
        "uiforgemax_add_workspace_root": (
            "Reactive fallback only: register an extra repo root (name != 'default') discovered "
            "mid-run, when 7_plan BLOCKs on a missing root name that wasn't known upfront. Prefer "
            "passing all known components to preflight/start_run instead of relying on this."
        ),
        "uiforgemax_start_run": (
            "Create session; requires workspace from preflight/set_workspace. Accepts the same "
            "`components='{...}'` JSON object as preflight if not already passed there."
        ),
        "uiforgemax_add_jira": (
            "Fetch Jira issue (REST/fixture) into inputs/; downloads attachments into "
            "inputs/attachments/ as source of truth. BLOCKS if listed attachments cannot "
            "be stored — preferred when Jira is configured or the user named a key"
        ),
        "uiforgemax_add_image": (
            "Copy wireframe/screenshot to inputs/attachments/ with role "
            "(reference|before|after|wireframe|mockup)"
        ),
        "uiforgemax_add_html": "Save HTML as SoT (inputs/page.html + inputs/attachments/)",
        "uiforgemax_add_prompt": (
            "Save free-text requirement only — blocked if text starts with an issue "
            "key while Jira is configured (use add_jira instead)"
        ),
        "uiforgemax_advance": "Run automatic stages until next gate, mediation pause, or completion",
        "uiforgemax_submit_mediation": "Submit IDE model JSON at model_mediation pause",
        "uiforgemax_approve_api": "Gate 1 approval",
        "uiforgemax_approve_understanding": "Legacy/no-op (understanding auto-passes)",
        "uiforgemax_approve_plan": "Sole human gate — locks plan then implement",
        "uiforgemax_request_changes": "Delta revise at plan approval (or normalize)",
        "uiforgemax_get_run_status": "Full run state + artifacts",
        "uiforgemax_get_pipeline_guide": "This guide + stage stats for a run",
        "uiforgemax_answer_clarifications": "Answer graph clarifications (structured)",
        "uiforgemax_list_runs": "List run ids",
        "uiforgemax_cancel_run": "Cancel run",
    },
}


def get_pipeline_guide(run_stats: dict | None = None) -> str:
    body = dict(PIPELINE_GUIDE)
    if run_stats:
        body["runStats"] = run_stats
    return json.dumps(body, indent=2)
