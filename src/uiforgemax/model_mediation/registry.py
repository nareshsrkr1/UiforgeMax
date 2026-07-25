"""Mediation kinds and per-stage IDE instructions."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from uiforgemax.env_flags import skip_mediation
from uiforgemax.state import RunState, Stage


class MediationKind(str, Enum):
    REQUEST_CLASSIFICATION = "REQUEST_CLASSIFICATION"
    INTAKE_RECONCILIATION = "INTAKE_RECONCILIATION"
    REQUIREMENT_ANALYSIS = "REQUIREMENT_ANALYSIS"
    VISUAL_INTERPRETATION = "VISUAL_INTERPRETATION"
    QUERY_STRATEGY = "QUERY_STRATEGY"
    GRAPH_EXPLAIN = "GRAPH_EXPLAIN"
    REQ_MAP_VALIDATION = "REQ_MAP_VALIDATION"
    PLAN_REFINEMENT = "PLAN_REFINEMENT"
    TEST_GENERATION = "TEST_GENERATION"
    TEST_ENV_RECOVERY = "TEST_ENV_RECOVERY"
    TASK_DECOMPOSITION = "TASK_DECOMPOSITION"
    VISUAL_VALIDATION = "VISUAL_VALIDATION"
    POST_IMPLEMENT_REVIEW = "POST_IMPLEMENT_REVIEW"


# Stages that pause for IDE model mediation after deterministic MCP work.
MEDIATION_BY_STAGE: dict[Stage, MediationKind] = {
    Stage.CLASSIFY: MediationKind.REQUEST_CLASSIFICATION,
    Stage.NORMALIZE: MediationKind.REQUIREMENT_ANALYSIS,
    Stage.GRAPH_QUERY_PLAN: MediationKind.QUERY_STRATEGY,
    Stage.REQUIREMENT_MAP: MediationKind.GRAPH_EXPLAIN,
    Stage.PLAN: MediationKind.PLAN_REFINEMENT,
    Stage.TEST: MediationKind.TEST_GENERATION,
    Stage.DECOMPOSE: MediationKind.TASK_DECOMPOSITION,
}

# Additional visual pass when image input exists (runs after REQUIREMENT_ANALYSIS)
VISUAL_STAGE = Stage.NORMALIZE
VISUAL_KIND = MediationKind.VISUAL_INTERPRETATION

def _input_images(run_dir: Path) -> list[str]:
    inputs = run_dir / "inputs"
    if not inputs.exists():
        return []
    exts = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}
    return sorted(
        str(p.relative_to(run_dir)).replace("\\", "/")
        for p in inputs.iterdir()
        if p.suffix.lower() in exts
    )


def _image_roles(run_dir: Path) -> list[dict[str, str]]:
    """Return [{path, role}] using inputs/images.json when present.

    Falls back to role="reference" for any image lacking an explicit role so
    before/after redesigns and multi-image intake carry through mediation.
    """
    manifest = run_dir / "inputs" / "images.json"
    roles: dict[str, str] = {}
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            for entry in data.get("images", []):
                path = str(entry.get("path", "")).replace("\\", "/")
                if path:
                    roles[path] = entry.get("role", "reference")
        except (json.JSONDecodeError, OSError):
            pass
    return [{"path": p, "role": roles.get(p, "reference")} for p in _input_images(run_dir)]


def _mediation_key(stage: Stage, kind: MediationKind) -> str:
    return f"{stage.value}::{kind.value}"


_EMBED_MAX_BYTES = 60_000
_EMBED_SUFFIXES = {".json", ".txt", ".md"}


def attach_artifact_contents(request: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """Return a copy of ``request`` with SMALL readArtifacts inlined under
    ``artifactContents``, so the driving agent does not need a separate
    (permission-prompting) file Read for each one.

    Only small text/JSON artifacts are embedded (``_EMBED_MAX_BYTES`` cap,
    ``_EMBED_SUFFIXES`` only) — large files like ``inputs/page.html`` and big
    ``graph/source-snapshots.json`` stay as paths (a single Read each, not
    part of the many-small-JSON prompt storm). The saved-to-disk request is
    left lean; only the agent-facing copy is enriched.
    """
    paths = request.get("readArtifacts") or []
    contents: dict[str, str] = {}
    for rel in paths:
        if not isinstance(rel, str):
            continue
        target = run_dir / rel
        try:
            if not target.is_file():
                continue
            if target.suffix.lower() not in _EMBED_SUFFIXES:
                continue
            if target.stat().st_size > _EMBED_MAX_BYTES:
                continue
            contents[rel] = target.read_text(encoding="utf-8")
        except OSError:
            continue
    if not contents:
        return request
    enriched = dict(request)
    enriched["artifactContents"] = contents
    enriched["artifactContentsNote"] = (
        "Small readArtifacts are inlined here. Use these directly — do NOT Read "
        "these files again. Only Read a readArtifacts path that is absent from "
        "artifactContents (large files like inputs/page.html or source snapshots)."
    )
    return enriched


def build_mediation_request(
    stage: Stage,
    kind: MediationKind,
    run_dir: Path,
    state: RunState,
) -> dict[str, Any]:
    images = _input_images(run_dir)
    image_roles = _image_roles(run_dir)
    base = {
        "stage": stage.value,
        "kind": kind.value,
        "mediationKey": _mediation_key(stage, kind),
        "submitTool": "uiforgemax_submit_mediation",
        "submitHint": (
            "For large payloads (e.g. PLAN_REFINEMENT with full file contents), "
            "write the JSON to a .json file in the run's mediation/ folder then call "
            "uiforgemax_submit_mediation with payload_file='mediation/<filename>.json' "
            "instead of passing the JSON inline as payload."
        ),
        "useIdeModel": True,
        "note": "Use Cursor/VS Code model — UiForgeMax MCP holds no LLM keys.",
    }

    if kind == MediationKind.REQUEST_CLASSIFICATION:
        return {
            **base,
            "instruction": (
                "Classify this request dynamically using your IDE model. Read the intake signals, "
                "any Jira ticket/prompt/HTML, and ALL images (note their roles — e.g. before vs after "
                "screens for a redesign). Decide the request type and the surfaces involved. This is "
                "open-ended: greenfield app, UI-only enhancement, API change, full-stack feature, "
                "before/after redesign, OpenFin/Electron/desktop app, migration, refactor, bugfix, or "
                "anything else the inputs indicate. The classification you return drives how the rest of "
                "the pipeline runs. Output JSON only — no repo edits. You may add fields beyond the "
                "schema if the request needs them."
            ),
            "readArtifacts": [
                "classification-signals.json",
                "inputs/jira.raw.json",
                "inputs/prompt.txt",
                "inputs/page.html",
                "inputs/attachments.json",
            ],
            "readImages": images,
            "imageRoles": image_roles,
            "outputSchema": {
                "requestType": "greenfield|enhancement|refactor|migration|bugfix|integration|unknown",
                "surface": "ui_only|api_only|full_stack|infra|unknown",
                "platform": ["web|openfin|electron|desktop|mobile|other"],
                "changeSignal": [
                    "before_after_screens|single_wireframe|prompt_only|jira_spec|existing_code"
                ],
                "targets": {"apps": ["string"], "domains": ["string"]},
                "capabilities": ["string"],
                "recommendedPolicy": "frontend_first|full_stack|extend_existing|contract_driven",
                "useGraph": "boolean — false for greenfield or empty repo",
                "runApi": "boolean — false for ui_only",
                "runVisual": "boolean — false for api_only without images",
                "greenfieldScaffold": "boolean — true to scaffold new app without graph",
                "rationale": "string",
                "confidence": "number",
                "needsHumanConfirmation": "boolean",
            },
        }

    if kind == MediationKind.REQUIREMENT_ANALYSIS:
        return {
            **base,
            "instruction": (
                "Analyze intake using your IDE model. Resolve Jira description vs comments/overrides "
                "(latest override wins). Output structured JSON only — no repo edits.\n\n"
                "SOURCE OF TRUTH: Read inputs/attachments.json and any inputs/page.html / images. "
                "HTML, wireframes, mockups, and design-note attachments are authoritative for UX, "
                "tokens, and layout — do not invent visuals that contradict them.\n\n"
                "CRITICAL — graphSearchStrategy (MANDATORY):\n"
                "The Graphify codebase index uses keyword-based queries to find relevant files. "
                "Your graphSearchStrategy drives ALL downstream queries — if you get this wrong, "
                "the pipeline searches for the wrong files and the plan will be incorrect.\n\n"
                "You MUST produce stack-aware, domain-specific search guidance:\n"
                "- intent: the overall search intent (theme_ui | ui | api | data_model | "
                "full_stack | infra | general)\n"
                "- filePatterns: file patterns likely relevant for THIS stack (e.g. '*.tsx' for "
                "React, '*.py' for Python, '*.go' for Go, '*.cs' for .NET — NOT hardcoded React)\n"
                "- componentNames: actual component/module/class names to search for based on "
                "the requirement (e.g. 'Sidebar', 'UserService', 'auth_middleware')\n"
                "- perAcSearch: for EACH acceptance criterion, provide targeted search terms "
                "that would find the files relevant to implementing that AC\n"
                "- architecturalPatterns: patterns specific to the detected stack (e.g. "
                "'pages/', 'components/', 'routes/' for React; 'models/', 'views/', 'urls.py' "
                "for Django; 'handlers/', 'services/' for Go)\n"
                "- keywords: 5-12 short search tokens specific to THIS requirement\n"
                "- focusFiles: likely relative paths if known from the requirement context\n"
                "- searchQueries: 1-3 ultra-short keyword strings (max ~60 chars each) that "
                "Graphify CLI will use — these should be specific to the domain, NOT generic\n\n"
                "DO NOT use generic keywords like 'component', 'page', 'style' unless the "
                "requirement is actually about those things. Be specific to what the ticket asks for."
            ),
            "readArtifacts": [
                "requirements.normalized.json",
                "inputs/jira.raw.json",
                "inputs/prompt.txt",
                "inputs/page.html",
                "inputs/attachments.json",
                "visual-spec.json",
                "request-classification.json",
            ],
            "readImages": images,
            "outputSchema": {
                "summary": "string",
                "scope": {"in": ["string"], "out": ["string"]},
                "overridesResolved": [{"comment": "string", "effect": "string"}],
                "assumptions": ["string"],
                "conflicts": ["string"],
                "acceptanceCriteria": [{"id": "string", "text": "string"}],
                "dataNeeds": [],
                "graphSearchStrategy": {
                    "intent": "theme_ui|ui|api|data_model|full_stack|infra|general",
                    "filePatterns": ["string — e.g. *.tsx, *.py, *.go — stack-specific"],
                    "componentNames": ["string — actual names to search for"],
                    "perAcSearch": [
                        {
                            "acId": "string",
                            "searchTerms": ["string — targeted terms for this AC"],
                            "expectedFileTypes": ["string — e.g. component, route, model, style"],
                        }
                    ],
                    "architecturalPatterns": ["string — stack-specific directory/file patterns"],
                    "keywords": ["string — 5-12 domain-specific search tokens"],
                    "focusFiles": ["string — likely relative paths"],
                    "searchQueries": [
                        "string — 1-3 ultra-short Graphify CLI keyword strings (max 60 chars)"
                    ],
                },
            },
        }

    if kind == MediationKind.VISUAL_INTERPRETATION:
        return {
            **base,
            "instruction": (
                "Read the actual image(s) from readImages NOW.  The visual-spec.json on disk is "
                "a provisional shell with visualSpecUnconfirmed=true and empty components/layout/tokens "
                "(sentinel value '__UNCONFIRMED_PENDING_MEDIATION__').  You MUST replace the entire "
                "shell with real data extracted from the image. Do NOT pass through the shell values.\n\n"
                "If imageRoles contains before/after pairs, diff them and describe exactly what "
                "changed (added/removed/restyled components, layout shifts, color/token changes).\n\n"
                "Extract from the image(s):\n"
                "- layout.regions: named regions with type (navigation, header, main, sidebar, etc.)\n"
                "- components[]: EVERY visible component — type, label/text, variant, colorHint, "
                "confidence. Do NOT invent components not visible in the image.\n"
                "- interactions[]: user actions inferred from the design (buttons, links, form submits)\n"
                "- visualTokens.colors[]: all distinct colors visible (hex or name + context)\n"
                "- visualTokens.typography[]: font sizes, weights, families if readable\n"
                "- visualTokens.spacing[]: gap/margin patterns if discernible\n"
                "- matchExactly: true if the image is a pixel-perfect reference the implementation "
                "must match exactly (look for 'match exactly', 'pixel perfect', or similar cues)\n"
                "- exactTextRequirements[]: button/label text that must appear verbatim\n"
                "- confidence: overall 0–1 score for how clearly the image is readable\n\n"
                "Set visualSpecUnconfirmed=false in your output to signal the spec is now confirmed. "
                "Output only valid JSON matching the outputSchema — no repo edits."
            ),
            "readArtifacts": ["requirements.normalized.json", "visual-spec.json", "request-classification.json"],
            "readImages": images,
            "imageRoles": image_roles,
            "outputSchema": {
                "sourceImage": "string — path to the primary image read",
                "visualSpecUnconfirmed": False,
                "matchExactly": "boolean",
                "beforeAfterDiff": {"added": ["string"], "removed": ["string"], "changed": ["string"]},
                "layout": {"regions": [{"id": "string", "type": "string", "confidence": 0.0}]},
                "components": [
                    {
                        "type": "string — exact component type e.g. Button, Table, Form, Navigation",
                        "label": "string (if applicable)",
                        "text": "string (if applicable)",
                        "variant": "string (if applicable)",
                        "colorHint": "string (if applicable)",
                        "confidence": 0.0,
                    }
                ],
                "interactions": [
                    {
                        "trigger": "string",
                        "expectedBehavior": "string",
                        "confidence": 0.0,
                        "needsConfirmation": False,
                    }
                ],
                "visualTokens": {
                    "colors": [{"value": "string", "context": "string"}],
                    "typography": [{"element": "string", "size": "string", "weight": "string"}],
                    "spacing": [{"context": "string", "value": "string"}],
                },
                "exactTextRequirements": ["string — text that must appear verbatim in the implementation"],
                "confidence": "number 0–1",
            },
        }

    if kind == MediationKind.QUERY_STRATEGY:
        return {
            **base,
            "instruction": (
                "Design the Graphify search strategy using your IDE model. You have the "
                "requirements, classification, sub-tasks (if decomposed), and the graph "
                "structure (graph/index.json shows what nodes/files exist in the codebase).\n\n"
                "Your job: decide EXACTLY what to search for in the codebase index. The "
                "deterministic query planner will use your output as its primary source — "
                "do not produce generic keywords.\n\n"
                "RULES:\n"
                "1. Read graph/index.json or graph/by-root/*/graph.json to understand what "
                "files and components actually exist in the codebase. Search terms must match "
                "real node labels/file paths in the graph.\n"
                "2. Read requirements.normalized.json for the graphSearchStrategy produced by "
                "REQUIREMENT_ANALYSIS — refine and validate it against what the graph actually "
                "contains.\n"
                "3. When sub-tasks exist (plans/subtasks.json), produce per-sub-task queries "
                "with focused search terms — NOT the same broad keywords for every sub-task.\n"
                "4. Each query must be a short keyword string (max 60 chars) that Graphify CLI "
                "can match against its index. Prefer node labels and file basenames over "
                "generic terms.\n"
                "5. Prioritize queries: most critical sub-task/AC first. Max 2 queries per "
                "sub-task, max 6 total.\n"
                "6. Include a lexicalKeywords list — terms for fast graph.json node scanning "
                "(the deterministic fallback that always runs before CLI queries).\n\n"
                "Output JSON — no repo edits."
            ),
            "readArtifacts": [
                "requirements.normalized.json",
                "request-classification.json",
                "plans/subtasks.json",
                "graph/index.json",
                "graph/by-root/default/graph.json",
                "graph/per-root.json",
                "visual-spec.json",
            ],
            "readImages": images,
            "outputSchema": {
                "queries": [
                    {
                        "id": "string — e.g. Q-ST-1-AUTH or Q-SIDEBAR",
                        "question": "string — short keyword search (max 60 chars)",
                        "reason": "string — what this query is looking for",
                        "subtaskId": "string|null — ST-N if sub-task-scoped",
                        "priority": "number 1-5",
                        "targetFileTypes": ["string — what kind of files to find"],
                    }
                ],
                "lexicalKeywords": ["string — terms for fast graph.json node scan"],
                "focusFiles": ["string — known file paths from graph to prioritize"],
                "strategy": "lexical_first|hybrid — lexical_first for simple theme/style tickets",
                "refinedFromRequirements": "boolean — true if you adjusted the graphSearchStrategy",
                "adjustments": ["string — what you changed from the original graphSearchStrategy"],
            },
        }

    if kind == MediationKind.GRAPH_EXPLAIN:
        return {
            **base,
            "instruction": (
                "Explain graph query results in plain language. Confirm reuse vs create vs API gaps. "
                "IMPORTANT: requirementMapAdjustments must structurally correct the map — "
                "use dropPaths / modify / create (not assumptions alone). Always drop "
                "package.json, project.json, lockfiles, tsconfig, vite config, README. "
                "Keep only real implementation files (source code, styles, templates — not config)."
            ),
            "readArtifacts": [
                "graph/queries.json",
                "graph/query-results.json",
                "graph/requirement-map.json",
                "requirements.normalized.json",
            ],
            "readImages": images,
            "outputSchema": {
                "explanations": [{"queryId": "string", "summary": "string"}],
                "requirementMapAdjustments": {
                    "assumptions": ["string"],
                    "clarificationsResolved": ["string"],
                    "dropPaths": ["string"],
                    "modify": [{"path": "string", "purpose": "string"}],
                    "create": [{"path": "string", "purpose": "string"}],
                    "executionOrder": ["string"],
                },
                "acceptanceMappingsReview": [{"acId": "string", "strategy": "string", "approved": True}],
            },
        }

    if kind == MediationKind.PLAN_REFINEMENT:
        return {
            **base,
            "instruction": (
                "Refine implementation-plan.json for THIS application (any stack/domain — not a "
                "demo template). Use requirement-map, GRAPH_EXPLAIN, and visuals. "
                "CRITICAL: for every create/modify that is not a known greenfield scaffold "
                "templateId, you MUST supply the full file `content` the MCP should write. "
                "MCP will not invent product-specific CSS/UI/API patches.\n"
                "SOURCE OF TRUTH: You MUST read inputs/attachments.json and any referenced "
                "HTML (inputs/page.html), wireframe/mockup images, and design-note markdown. "
                "Populate plan.sourceOfTruth and visualCompliance.referenceHtml / "
                "referenceImage / referenceArtifacts with those run-relative paths. Layout, "
                "tokens, fonts, and copy must follow those artifacts — do not invent a "
                "competing visual system.\n"
                "You are NOT allowed to read target-project files yourself, and Graphify's "
                "graph.json is structural only (imports/declarations) — it has no literal "
                "source text (no CSS selectors, no JSX body). To solve this, MCP itself read "
                "the CURRENT text of every modify/reuse/create candidate and put it in "
                "graph/source-snapshots.json BEFORE this mediation. For any 'modify' entry "
                "with exists=true there, use that content as the base: apply only the "
                "requirement's change and return the FULL edited file, preserving unrelated "
                "code/CSS rules exactly. Never fabricate a full-file replacement for a file "
                "whose current content you have not read from source-snapshots.json — if an "
                "entry is missing/truncated/binary there, keep the change minimal and note the "
                "gap in risks[] instead of guessing.\n"
                "validationPlan: leave unit paths empty (TEST_GENERATION chooses pytest/"
                "vitest/junit/…). Include topology, risks, blockers, coverage expectation. "
                "Output full plan JSON."
            ),
            "readArtifacts": [
                "plans/implementation-plan.json",
                "plans/subtasks.json",
                "graph/source-snapshots.json",
                "graph/requirement-map.json",
                "graph/mediation-explain.json",
                "requirements.normalized.json",
                "visual-spec.json",
                "inputs/attachments.json",
                "inputs/page.html",
                "mediation",
            ],
            "readImages": images,
            "outputSchema": {
                "summary": "string",
                "topology": {
                    "targetApp": "string",
                    "targetDomain": "string",
                    "surface": "string",
                    "roots": {},
                },
                "create": [
                    {
                        "path": "string",
                        "purpose": "string",
                        "templateId": "greenfield.*|omit",
                        "content": "REQUIRED full file body for real apps (not scaffold)",
                        "root": "default",
                    }
                ],
                "modify": [
                    {
                        "path": "string",
                        "purpose": "string",
                        "patchId": "omit for real apps",
                        "content": "REQUIRED full file body after your edit",
                        "root": "default",
                    }
                ],
                "executionOrder": ["string"],
                "sourceOfTruth": {
                    "primaryHtml": "inputs/page.html|null",
                    "primaryImage": "inputs/attachments/…|null",
                    "designNotes": ["string"],
                    "attachments": [{"filename": "string", "role": "string", "path": "string"}],
                },
                "risks": ["string"],
                "blockers": ["string"],
                "validationPlan": {
                    "filesUnderTest": ["string"],
                    "unit": ["path/to/file.test.ext"],
                    "unitSpecs": [{"path": "string", "covers": "string", "type": "unit"}],
                    "automated": {
                        "unitTests": True,
                        "codeCoverage": True,
                        "coverageThreshold": {"lines": 70, "soft": True},
                    },
                    "staticChecks": ["string"],
                    "manualChecks": ["string"],
                },
                "tests": {
                    "unit": ["string"],
                    "staticChecks": ["string"],
                    "manualChecks": ["string"],
                    "automated": {},
                    "compliance": {},
                },
                "visualCompliance": {
                    "referenceHtml": "inputs/page.html|null",
                    "referenceImage": "string",
                    "referenceArtifacts": [{"filename": "string", "role": "string", "path": "string"}],
                    "checks": [],
                },
            },
        }

    if kind == MediationKind.TEST_GENERATION:
        return {
            **base,
            "instruction": (
                "YOU OWN the entire test strategy — including any install steps needed so "
                "run[] can succeed. MCP does not choose the test framework and does not invent "
                "a fixed install workflow; it executes your installHints[] + run[].\n"
                "Detect the project stack and pick the right test framework:\n"
                "• Python → pytest (or unittest) + test_*.py / tests/\n"
                "• Java/Kotlin → JUnit/TestNG via mvn/gradle + src/test/java\n"
                "• Go → go test + *_test.go\n"
                "• .NET/C# → xUnit/NUnit via dotnet test\n"
                "• Rust → cargo test + #[test] in src/ or tests/\n"
                "• Ruby → RSpec/Minitest\n"
                "• PHP → PHPUnit\n"
                "• Node/TS/JS → Vitest/Jest + *.test.ts (use Testing Library for component tests)\n"
                "• Angular → Karma/Jest + *.spec.ts (use TestBed for component fixtures)\n"
                "• Vue → Vitest + @vue/test-utils\n"
                "• Svelte → Vitest + @testing-library/svelte\n"
                "• OpenFin → same as detected UI framework + fin API mocks if needed\n"
                "For visual ACs (layout, dark mode, contrast): add Playwright e2e tests. Mark "
                "Playwright run[] with suite='playwright' (optional). MCP tries ONE project-local "
                "install; if unavailable, Playwright is soft-skipped — unit tests still decide pass.\n"
                "If deps are not installed, include installHints[] in THIS response "
                "(npm install / pip install / mvn dependency:resolve / dotnet restore / …) — "
                "do not assume MCP PATH has tools or that packages are already installed.\n"
                "When emitting DOM tests that import @testing-library/react (or vue/svelte), "
                "also install peer @testing-library/dom if the project does not already have it "
                "(include installHints). Same idea for other stacks: declare peers the tests need.\n"
                "Return tests[] bodies AND run[] (cwd/root correct for monorepos). "
                "Every tests[].path you emit MUST appear in some run[] command.\n"
                "Keep this stack-agnostic — pick the framework from the repo, not a fixed template.\n"
                "MCP only writes + executes."
            ),
            "readArtifacts": [
                "plans/approved-plan.json",
                "plans/implementation-plan.json",
                "implementation/diff-summary.json",
                "request-classification.json",
                "run-flow.json",
                "visual-spec.json",
            ],
            "readImages": images,
            "outputSchema": {
                "stack": {
                    "language": "java|typescript|javascript|python|csharp|go|other",
                    "testFramework": "junit|testng|pytest|vitest|jest|xunit|go test|playwright|other",
                    "buildTool": "maven|gradle|npm|pnpm|poetry|dotnet|go|other|none",
                },
                "installHints": [
                    {
                        "command": "string — e.g. npm install at workspaces root; npm install -D @playwright/test",
                        "cwd": ".",
                        "root": "default",
                        "purpose": "string",
                    }
                ],
                "tests": [
                    {
                        "path": "string",
                        "content": "string",
                        "type": "unit|integration|dom|snapshot|playwright",
                        "covers": "string",
                        "root": "default",
                    }
                ],
                "run": [
                    {
                        "command": "string — e.g. pytest tests/; npx vitest run; dotnet test; go test ./...",
                        "cwd": ".",
                        "root": "default",
                        "coverage": False,
                        "suite": "unit|dom|playwright — use playwright for e2e (optional soft)",
                        "required": "false for playwright unless you must hard-fail without browser",
                    }
                ],
                "coverage": {
                    "required": True,
                    "linesThreshold": 70,
                    "soft": True,
                    "summaryGlob": "optional path to coverage report if known",
                },
            },
        }

    if kind == MediationKind.TEST_ENV_RECOVERY:
        return {
            **base,
            "instruction": (
                "YOU decide the recovery steps for THIS stack (any language/framework — not "
                "Angular-only, not Node-only). MCP does not invent a fixed workflow — it only "
                "executes your allowlisted installHints[] and run[].\n"
                "REQUIRED reading: tests/toolchain-facts.json + tests/command-logs.json "
                "(full stdout/stderr) + tests/env-gap.json.\n"
                "Rules:\n"
                "1) Read command-logs first. If you see Cannot find module / ModuleNotFoundError / "
                "No module named / missingPackages → return installHints for THOSE packages "
                "(e.g. npm install -D @testing-library/dom, pip install <pkg>, etc.) then a "
                "corrected run[]. skipTests is REJECTED until install was attempted.\n"
                "2) If needInstallAny / root.needInstall / missingPackages → installHints[] "
                "REQUIRED (copy suggestedInstall when present). Empty installHints is REJECTED.\n"
                "3) If hoistedRunner / avoidLocalRunnerPath → do NOT call "
                "./node_modules/<runner> under the app; use npm exec/npx from app or "
                "node <runnerPath> with cwd at jsInstallRoot.\n"
                "4) Prefer absolute node/npm from facts.node / facts.npm when PATH is empty.\n"
                "5) System installs (winget/choco) only if needed and "
                "UIFORGEMAX_ALLOW_TOOL_INSTALL=1.\n"
                "6) You may also rewrite tests[] (fix bad imports/mocks) when the log shows "
                "a test-authoring issue, not only install.\n"
                "7) skipTests=true only after install/run strategies are exhausted, with "
                "skipReason that cites the log.\n"
                "8) Playwright: if suite=playwright failed/unavailable, prefer installHints "
                "(npm install -D @playwright/test + npx playwright install chromium at "
                "installRootRel) once; MCP soft-skips further Playwright if still missing.\n"
                "Do not claim deps are installed when runnerAtInstallRoot is false or "
                "missingPackages is non-empty."
            ),
            "readArtifacts": [
                "tests/toolchain-facts.json",
                "tests/command-logs.json",
                "tests/env-gap.json",
                "tests/install-log.json",
                "tests/unit-results.json",
                "tests/generated-tests.json",
                "plans/approved-plan.json",
                "plans/implementation-plan.json",
                "request-classification.json",
            ],
            "readImages": images,
            "outputSchema": {
                "diagnosis": "string",
                "stack": {
                    "language": "string",
                    "testFramework": "string",
                    "buildTool": "string",
                },
                "run": [
                    {
                        "command": "string",
                        "cwd": ".",
                        "root": "default",
                        "coverage": False,
                    }
                ],
                "installHints": [
                    {
                        "command": "string",
                        "cwd": ".",
                        "root": "default",
                        "purpose": "string",
                    }
                ],
                "tests": [
                    {
                        "path": "string",
                        "content": "string",
                        "type": "unit|integration|dom|snapshot",
                        "covers": "string",
                        "root": "default",
                    }
                ],
                "coverage": {
                    "required": True,
                    "linesThreshold": 70,
                    "soft": True,
                },
                "skipTests": False,
                "skipReason": "string",
            },
        }

    if kind == MediationKind.TASK_DECOMPOSITION:
        return {
            **base,
            "instruction": (
                "Decompose this requirement into independent sub-tasks for implementation. "
                "Each sub-task should be a coherent unit of work that can be implemented, "
                "tested, and validated independently.\n\n"
                "STRATEGY SELECTION (pick based on requestType + surface from classification):\n"
                "- visual_regions: UI with wireframes/mockups — split by layout regions "
                "(header, sidebar, content, footer, modals)\n"
                "- ac_grouping: general — group related ACs sharing data/components\n"
                "- dependency_graph: full-stack — separate data models → API routes → "
                "middleware → UI consumers\n"
                "- domain_split: API/backend — split by domain entity or service boundary "
                "(e.g. 'users CRUD', 'auth middleware', 'notification service')\n\n"
                "RULES:\n"
                "1. Every acceptance criterion MUST be linked to exactly one sub-task.\n"
                "2. Sub-tasks should align with visual regions when images/wireframes exist.\n"
                "3. Dependencies between sub-tasks must be explicit (e.g., ST-2 depends on ST-1 "
                "if ST-2's component imports from ST-1's output).\n"
                "4. Each sub-task gets focusKeywords (5-10 short tokens) that will drive "
                "targeted Graphify queries — do NOT reuse the same broad keywords across "
                "all sub-tasks. These must be specific to the domain and stack.\n"
                "5. Each sub-task gets searchContext: componentNames (actual names to search for), "
                "filePatterns (expected file patterns for this stack, e.g. *.py for Python, *.tsx "
                "for React), and searchQueries (1-2 short keyword strings for Graphify CLI). "
                "This drives the per-sub-task graph queries.\n"
                "6. Order by dependency (leaves first), then by priority.\n"
                "7. For API+UI work, prefer separating API sub-tasks from UI sub-tasks.\n"
                "8. surfaceHint per sub-task: ui_only, api_only, full_stack.\n"
                "9. estimatedFiles: your best guess at files this sub-task will touch — "
                "refine later in planning.\n\n"
                "Output JSON matching outputSchema — no repo edits."
            ),
            "readArtifacts": [
                "requirements.normalized.json",
                "visual-spec.json",
                "request-classification.json",
                "classification-signals.json",
            ],
            "readImages": images,
            "imageRoles": image_roles,
            "outputSchema": {
                "decompositionStrategy": "visual_regions|ac_grouping|dependency_graph|domain_split",
                "subtasks": [
                    {
                        "id": "ST-N",
                        "title": "string",
                        "summary": "string",
                        "surfaceHint": "ui_only|api_only|full_stack",
                        "linkedAcIds": ["AC-1"],
                        "dependencies": ["ST-M"],
                        "visualRegion": {
                            "regionId": "string",
                            "regionType": "string",
                            "description": "string",
                        },
                        "priority": "number 1-5",
                        "focusKeywords": ["string — 5-10 domain-specific search tokens"],
                        "searchContext": {
                            "componentNames": ["string — actual class/module/component names"],
                            "filePatterns": ["string — e.g. *.tsx, *.py, routes/*.go"],
                            "searchQueries": ["string — 1-2 short Graphify CLI keyword strings"],
                        },
                        "estimatedFiles": ["string"],
                        "complexity": "low|medium|high",
                    }
                ],
            },
        }

    if kind == MediationKind.INTAKE_RECONCILIATION:
        return {
            **base,
            "instruction": (
                "Reconcile conflicting intake data using your IDE model. Jira tickets often have "
                "description edits, comment overrides, field-level changes, and attachments that "
                "contradict each other. Your job is to produce ONE authoritative requirement set.\n\n"
                "RULES:\n"
                "1. Read ALL inputs: jira.raw.json (description + comments + changelog), prompt.txt, "
                "page.html, attachments.json, and any images.\n"
                "2. Comments are chronological — later comments override earlier ones when they "
                "conflict. Explicit overrides ('override:', 'change X to Y', 'don't use X') "
                "always win over the original description.\n"
                "3. Jira field edits (e.g. priority changed, labels added, status transitions) "
                "provide context — note them but don't let old field values override explicit "
                "comment instructions.\n"
                "4. Flag unresolvable contradictions (e.g. two recent comments that disagree) "
                "as 'needsHumanClarification' — the pipeline will surface these.\n"
                "5. Produce a clean, reconciled summary with the final agreed requirements.\n"
                "6. Track what was overridden and by whom/when for audit trail.\n\n"
                "Output JSON only — no repo edits."
            ),
            "readArtifacts": [
                "inputs/jira.raw.json",
                "inputs/prompt.txt",
                "inputs/page.html",
                "inputs/attachments.json",
            ],
            "readImages": images,
            "imageRoles": image_roles,
            "outputSchema": {
                "reconciledSummary": "string — the final agreed requirement after resolving conflicts",
                "overrides": [
                    {
                        "source": "string — comment/field-edit/attachment",
                        "author": "string",
                        "timestamp": "string",
                        "what": "string — what was overridden",
                        "from": "string — original value",
                        "to": "string — new value",
                    }
                ],
                "contradictions": [
                    {
                        "field": "string",
                        "sources": ["string — which inputs disagree"],
                        "resolved": "boolean",
                        "resolution": "string — how it was resolved (or null if unresolved)",
                    }
                ],
                "needsHumanClarification": [
                    {
                        "question": "string",
                        "context": "string — why this is ambiguous",
                    }
                ],
                "inputSources": ["string — which inputs were present and read"],
                "confidence": "number 0-1",
            },
        }

    if kind == MediationKind.REQ_MAP_VALIDATION:
        return {
            **base,
            "instruction": (
                "Validate requirement-map coverage using your IDE model. After graph queries "
                "mapped evidence to ACs, verify the mapping is complete and correct.\n\n"
                "CHECKS:\n"
                "1. Every AC must have at least one mapped file (modify or create). Flag any "
                "unmapped ACs as coverage gaps.\n"
                "2. Mapped files must actually be relevant to their AC — flag false positives "
                "(e.g. config files mapped to UI ACs).\n"
                "3. Check for missing dependencies — if AC-2 needs a component that AC-1 "
                "creates, the execution order must reflect that.\n"
                "4. Verify file paths look reasonable for the detected stack/architecture.\n"
                "5. When sub-tasks exist, validate that per-sub-task evidence is focused "
                "(not overlapping broadly across sub-tasks).\n"
                "6. Suggest any files the graph missed that should be included based on "
                "the requirements (e.g. style files for visual ACs, test files for tested ACs).\n\n"
                "Output JSON — no repo edits."
            ),
            "readArtifacts": [
                "graph/requirement-map.json",
                "graph/query-results.json",
                "requirements.normalized.json",
                "request-classification.json",
                "plans/subtasks.json",
                "visual-spec.json",
            ],
            "readImages": images,
            "outputSchema": {
                "coverageScore": "number 0-1 — overall coverage of ACs",
                "acCoverage": [
                    {
                        "acId": "string",
                        "covered": "boolean",
                        "mappedFiles": ["string"],
                        "gaps": ["string — what's missing"],
                        "falsePositives": ["string — files that shouldn't be mapped here"],
                    }
                ],
                "missingFiles": [
                    {
                        "path": "string — suggested file path",
                        "purpose": "string",
                        "forAcIds": ["string"],
                        "action": "create|modify",
                    }
                ],
                "executionOrderIssues": ["string — dependency ordering problems"],
                "subtaskOverlap": ["string — sub-tasks with too much file overlap"],
                "approved": "boolean — true if coverage is sufficient to proceed",
                "adjustments": {
                    "addToModify": [{"path": "string", "purpose": "string"}],
                    "addToCreate": [{"path": "string", "purpose": "string"}],
                    "dropPaths": ["string"],
                    "reorderExecution": ["string — corrected execution order"],
                },
            },
        }

    if kind == MediationKind.POST_IMPLEMENT_REVIEW:
        return {
            **base,
            "instruction": (
                "Review the implemented code against the approved plan using your IDE model. "
                "This catches logic errors, missing implementations, and plan deviations that "
                "visual validation cannot detect (API routes, state management, data flow).\n\n"
                "CHECKS:\n"
                "1. Every create/modify action in the plan must have a corresponding file in "
                "diff-summary. Flag any plan actions that were not implemented.\n"
                "2. For each implemented file, verify the content matches the plan's intent "
                "(correct component structure, API endpoints, data models, etc.).\n"
                "3. Check for obvious logic errors: unused imports, undefined references, "
                "missing error handling at system boundaries, broken data flow between files.\n"
                "4. Verify acceptance criteria coverage — can each AC be satisfied by the "
                "implemented code? Flag ACs that appear uncovered.\n"
                "5. Check cross-file consistency: imports match exports, API routes match "
                "client calls, state management connects correctly.\n"
                "6. For sub-tasked implementations, verify sub-task boundaries are clean "
                "(no circular dependencies between sub-task outputs).\n\n"
                "Output JSON — no repo edits."
            ),
            "readArtifacts": [
                "plans/approved-plan.json",
                "plans/implementation-plan.json",
                "plans/subtasks.json",
                "implementation/diff-summary.json",
                "requirements.normalized.json",
                "graph/requirement-map.json",
            ],
            "readImages": images,
            "outputSchema": {
                "overallQuality": "number 0-1",
                "planCoverage": {
                    "totalActions": "number",
                    "implemented": "number",
                    "missing": ["string — paths not implemented"],
                    "extra": ["string — files written but not in plan"],
                },
                "acCoverage": [
                    {
                        "acId": "string",
                        "covered": "boolean",
                        "implementedBy": ["string — file paths"],
                        "gaps": ["string — what's missing"],
                    }
                ],
                "issues": [
                    {
                        "severity": "critical|major|minor",
                        "file": "string",
                        "issue": "string",
                        "suggestedFix": "string",
                    }
                ],
                "crossFileIssues": [
                    {
                        "files": ["string"],
                        "issue": "string",
                        "severity": "critical|major|minor",
                    }
                ],
                "passesReview": "boolean — true if no critical issues",
                "subtaskIntegrity": [
                    {
                        "subtaskId": "string",
                        "status": "complete|partial|missing",
                        "issues": ["string"],
                    }
                ],
            },
        }

    if kind == MediationKind.VISUAL_VALIDATION:
        return {
            **base,
            "instruction": (
                "Compare the implemented files against the original wireframe/mockup images. "
                "For each sub-task, assess visual fidelity:\n\n"
                "1. Read the original images from readImages.\n"
                "2. Read the implementation file contents from the approved plan and "
                "implementation diff-summary.\n"
                "3. For each sub-task, score fidelity 0-1 and list specific deviations.\n"
                "4. Flag sub-tasks that need delta re-implementation (fidelity < 0.7 or "
                "critical deviations like wrong colors, missing components, layout breaks).\n"
                "5. For flagged sub-tasks, provide specific fix instructions.\n\n"
                "Output JSON — no repo edits."
            ),
            "readArtifacts": [
                "visual-spec.json",
                "plans/subtasks.json",
                "plans/approved-plan.json",
                "plans/implementation-plan.json",
                "implementation/diff-summary.json",
                "inputs/attachments.json",
            ],
            "readImages": images,
            "imageRoles": image_roles,
            "outputSchema": {
                "overallFidelity": "number 0-1",
                "subtaskResults": [
                    {
                        "subtaskId": "ST-N",
                        "fidelity": "number 0-1",
                        "deviations": [
                            {
                                "severity": "critical|major|minor",
                                "component": "string",
                                "expected": "string",
                                "actual": "string",
                                "fix": "string",
                            }
                        ],
                        "needsReimplementation": "boolean",
                        "fixInstructions": "string — specific changes needed",
                        "affectedFiles": ["string"],
                    }
                ],
                "passesVisualGate": "boolean",
                "globalIssues": ["string — cross-sub-task visual problems"],
            },
        }

    return base


def _has_jira_with_comments(run_dir: Path) -> bool:
    """True when Jira input exists and has comments or changelog entries."""
    jira_path = run_dir / "inputs" / "jira.raw.json"
    if not jira_path.exists():
        return False
    try:
        data = json.loads(jira_path.read_text(encoding="utf-8"))
        comments = data.get("comments") or data.get("fields", {}).get("comment", {}).get("comments", [])
        changelog = data.get("changelog") or data.get("fields", {}).get("changelog", {}).get("histories", [])
        return bool(comments or changelog)
    except (json.JSONDecodeError, OSError):
        return False


def pending_mediations(stage: Stage, run_dir: Path, state: RunState) -> list[tuple[Stage, MediationKind]]:
    """Mediation steps due after deterministic work at ``stage``."""
    from uiforgemax.pipeline.flow_router import flow_flags
    from uiforgemax.pipeline.testing import env_recovery_pending

    flags = flow_flags(run_dir, state)
    pending: list[tuple[Stage, MediationKind]] = []
    primary = MEDIATION_BY_STAGE.get(stage)

    # NORMALIZE: prepend INTAKE_RECONCILIATION when Jira has comments/edits
    if stage == Stage.NORMALIZE and _has_jira_with_comments(run_dir):
        pending.append((stage, MediationKind.INTAKE_RECONCILIATION))

    if primary:
        if primary == MediationKind.GRAPH_EXPLAIN and not flags.get("useGraph", True):
            pass
        elif primary == MediationKind.TASK_DECOMPOSITION:
            from uiforgemax.pipeline.decompose import should_auto_decompose
            reqs_path = run_dir / "requirements.normalized.json"
            if reqs_path.exists():
                try:
                    reqs = json.loads(reqs_path.read_text(encoding="utf-8"))
                    cls_path = run_dir / "request-classification.json"
                    cls = json.loads(cls_path.read_text(encoding="utf-8")) if cls_path.exists() else {}
                    if not should_auto_decompose(reqs, cls):
                        pending.append((stage, primary))
                except (json.JSONDecodeError, OSError):
                    pending.append((stage, primary))
            else:
                pending.append((stage, primary))
        else:
            pending.append((stage, primary))
    if stage == VISUAL_STAGE and _input_images(run_dir) and flags.get("runVisual", True):
        pending.append((stage, VISUAL_KIND))

    # REQUIREMENT_MAP: append coverage validation after GRAPH_EXPLAIN
    if stage == Stage.REQUIREMENT_MAP:
        pending.append((stage, MediationKind.REQ_MAP_VALIDATION))

    if stage == Stage.VISUAL_VALIDATE and _input_images(run_dir):
        pending.append((stage, MediationKind.VISUAL_VALIDATION))

    # IMPLEMENT: post-implementation code review
    if stage == Stage.IMPLEMENT:
        pending.append((stage, MediationKind.POST_IMPLEMENT_REVIEW))

    # After TEST_GENERATION: recover when a tool/env gap was recorded.
    if stage == Stage.TEST and env_recovery_pending(run_dir):
        pending.append((stage, MediationKind.TEST_ENV_RECOVERY))
    return pending
