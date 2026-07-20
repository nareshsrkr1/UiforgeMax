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
    REQUIREMENT_ANALYSIS = "REQUIREMENT_ANALYSIS"
    VISUAL_INTERPRETATION = "VISUAL_INTERPRETATION"
    GRAPH_EXPLAIN = "GRAPH_EXPLAIN"
    PLAN_REFINEMENT = "PLAN_REFINEMENT"
    TEST_GENERATION = "TEST_GENERATION"
    TEST_ENV_RECOVERY = "TEST_ENV_RECOVERY"


# Stages that pause for IDE model mediation after deterministic MCP work.
MEDIATION_BY_STAGE: dict[Stage, MediationKind] = {
    Stage.CLASSIFY: MediationKind.REQUEST_CLASSIFICATION,
    Stage.NORMALIZE: MediationKind.REQUIREMENT_ANALYSIS,
    Stage.REQUIREMENT_MAP: MediationKind.GRAPH_EXPLAIN,
    Stage.PLAN: MediationKind.PLAN_REFINEMENT,
    Stage.TEST: MediationKind.TEST_GENERATION,
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
                "IMPORTANT — graphSearchHints: Graphify NL queries are slow when long. "
                "Always include graphSearchHints with:\n"
                "- intent: theme_ui | ui | api | general\n"
                "- keywords: 3–8 short search tokens (e.g. dark, sidebar, styles.css, App.tsx)\n"
                "- focusFiles: likely relative paths if known\n"
                "- shortQuestions: 0–2 ultra-short keyword strings (max ~60 chars each), "
                "NOT full sentences. Example: \"dark sidebar styles.css App.tsx\""
            ),
            "readArtifacts": [
                "requirements.normalized.json",
                "inputs/jira.raw.json",
                "inputs/prompt.txt",
                "inputs/page.html",
                "inputs/attachments.json",
                "visual-spec.json",
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
                "graphSearchHints": {
                    "intent": "theme_ui|ui|api|general",
                    "keywords": ["string"],
                    "focusFiles": ["string"],
                    "shortQuestions": ["string"],
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

    if kind == MediationKind.GRAPH_EXPLAIN:
        return {
            **base,
            "instruction": (
                "Explain graph query results in plain language. Confirm reuse vs create vs API gaps. "
                "IMPORTANT: requirementMapAdjustments must structurally correct the map — "
                "use dropPaths / modify / create (not assumptions alone). Always drop "
                "package.json, project.json, lockfiles, tsconfig, vite config, README. "
                "Keep only real implementation files (e.g. App.tsx, styles, pages)."
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
                    "unit": ["path/to/File.test.tsx"],
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
                "run[] can succeed. MCP does not choose pytest vs vitest vs junit and does "
                "not invent a fixed install workflow; it executes your installHints[] + run[].\n"
                "• Python → pytest (or project runner) + test_*.py / tests/\n"
                "• Java → mvn/gradle + src/test/java\n"
                "• Node/React UI → REQUIRED: Vitest/Jest + Testing Library DOM tests "
                "(/** @vitest-environment jsdom */, run those files in run[]). ALSO add "
                "Playwright e2e when ACs are visual (dark mode, layout, contrast): "
                "tests type='playwright', run[] with suite='playwright' (optional by default). "
                "Install at npm workspaces root (cwd=../..). MCP will try ONE project-local "
                "`npm install -D @playwright/test` + `npx playwright install chromium`; if that "
                "fails, Playwright is soft-skipped and status is written to "
                "tests/playwright-status.json — unit/DOM still decide pass/fail.\n"
                "• Go → go test; .NET → dotnet test; etc.\n"
                "If deps are not installed, include installHints[] in THIS response "
                "(npm install / poetry install / mvnw dependency:resolve / …) — do not assume "
                "MCP PATH has tools or that node_modules already exists.\n"
                "Return tests[] bodies AND run[] (cwd/root correct for monorepos). "
                "Every tests[].path you emit MUST appear in some run[] command — do not write "
                "DOM/Playwright files then only run string/source checks.\n"
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
                        "command": "string — e.g. npx vitest run src/App.test.tsx; npx playwright test",
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
                "YOU decide the recovery steps for THIS stack. MCP does not invent a fixed "
                "workflow — it only executes your allowlisted installHints[] and run[].\n"
                "REQUIRED reading: tests/toolchain-facts.json (authoritative).\n"
                "Rules:\n"
                "1) If needInstallAny / root.needInstall is true → you MUST return installHints[] "
                "(use suggestedInstall cwd/command, e.g. npm install at monorepo installRootRel). "
                "Empty installHints in that case is REJECTED.\n"
                "2) If hoistedRunner / avoidLocalRunnerPath is set → do NOT call "
                "./node_modules/<runner> under the app; use npm exec/npx from app or "
                "node <runnerPath> with cwd at jsInstallRoot.\n"
                "3) Prefer absolute node/npm from facts.node / facts.npm when PATH is empty.\n"
                "4) System installs (winget/choco) only if needed and "
                "UIFORGEMAX_ALLOW_TOOL_INSTALL=1.\n"
                "5) Or skipTests=true with skipReason.\n"
                "6) Playwright: if suite=playwright failed/unavailable, prefer installHints "
                "(npm install -D @playwright/test + npx playwright install chromium at "
                "installRootRel) once; MCP soft-skips further Playwright if still missing.\n"
                "Do not claim deps are installed when runnerAtInstallRoot is false."
            ),
            "readArtifacts": [
                "tests/toolchain-facts.json",
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

    return base


def pending_mediations(stage: Stage, run_dir: Path, state: RunState) -> list[tuple[Stage, MediationKind]]:
    """Mediation steps due after deterministic work at ``stage``."""
    from uiforgemax.pipeline.flow_router import flow_flags
    from uiforgemax.pipeline.testing import env_recovery_pending

    flags = flow_flags(run_dir, state)
    pending: list[tuple[Stage, MediationKind]] = []
    primary = MEDIATION_BY_STAGE.get(stage)
    if primary:
        if primary == MediationKind.GRAPH_EXPLAIN and not flags.get("useGraph", True):
            pass
        else:
            pending.append((stage, primary))
    if stage == VISUAL_STAGE and _input_images(run_dir) and flags.get("runVisual", True):
        pending.append((stage, VISUAL_KIND))
    # After TEST_GENERATION: recover when a tool/env gap was recorded.
    if stage == Stage.TEST and env_recovery_pending(run_dir):
        pending.append((stage, MediationKind.TEST_ENV_RECOVERY))
    return pending
