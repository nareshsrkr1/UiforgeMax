"""Drop non-implementation graph noise from create/modify/reuse lists.

Graphify often returns package.json / project.json / README as evidence.
Those must not become Gate-2/3 edit targets — the flow corrects this
automatically (no human cleanup).
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

# Metadata / tooling — never treat as implementation touch-points.
_NON_IMPL_NAMES = frozenset(
    {
        # JS/TS ecosystem
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
        "project.json", "nx.json", "workspace.json",
        "tsconfig.json", "tsconfig.base.json", "tsconfig.app.json", "tsconfig.spec.json",
        "jsconfig.json", "angular.json",
        "vite.config.ts", "vite.config.js", "vite.config.mts",
        "next.config.js", "next.config.mjs", "nuxt.config.ts", "nuxt.config.js",
        "webpack.config.js", "webpack.config.ts",
        "jest.config.ts", "jest.config.js", "vitest.config.ts",
        "eslint.config.js", "eslint.config.mjs", ".eslintrc.json",
        "svelte.config.js", "tailwind.config.js", "tailwind.config.ts",
        "postcss.config.js", "postcss.config.cjs",
        # Python
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "poetry.lock", "pipfile", "pipfile.lock",
        # Java/Kotlin
        "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
        "gradle.properties", "gradlew", "gradlew.bat",
        # Go
        "go.mod", "go.sum",
        # Rust
        "cargo.toml", "cargo.lock",
        # .NET
        "*.csproj", "*.sln", "nuget.config",
        # Ruby
        "gemfile", "gemfile.lock", "rakefile",
        # PHP
        "composer.json", "composer.lock",
        # General
        ".gitignore", ".editorconfig", ".prettierrc", ".prettierrc.json",
        "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "makefile", "cmakelists.txt",
        "readme.md", "readme", "readme.txt",
        "license", "license.md", "license.txt",
        "changelog.md", "contributing.md",
    }
)

_IMPL_SUFFIXES = (
    ".tsx", ".ts", ".jsx", ".js",
    ".css", ".scss", ".sass", ".less",
    ".html", ".vue", ".svelte",
    ".py", ".go", ".rs", ".java", ".kt", ".cs", ".rb", ".php",
)


def is_non_implementation_path(path: str | None) -> bool:
    if not path:
        return True
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    return name in _NON_IMPL_NAMES


def is_implementation_path(path: str | None) -> bool:
    if not path or is_non_implementation_path(path):
        return False
    lower = path.replace("\\", "/").lower()
    return lower.endswith(_IMPL_SUFFIXES)


def filter_impl_paths(paths: list[str]) -> list[str]:
    """Prefer real code/style files; drop config/lock/readme noise."""
    impl = [p for p in paths if is_implementation_path(p)]
    if impl:
        return impl
    # If graph only returned noise, return empty so later stages ask for mediation
    # rather than planning edits to package.json.
    return []


def sanitize_requirement_map(req_map: dict[str, Any]) -> dict[str, Any]:
    """Rewrite modify/create/reuse/executionOrder to implementation files only."""
    create = [f for f in (req_map.get("create") or []) if is_implementation_path(f.get("path"))]
    modify = [f for f in (req_map.get("modify") or []) if is_implementation_path(f.get("path"))]
    reuse = [r for r in (req_map.get("reuse") or []) if is_implementation_path(r.get("path"))]

    dropped = []
    for f in (req_map.get("modify") or []) + (req_map.get("create") or []):
        p = f.get("path")
        if p and is_non_implementation_path(p):
            dropped.append(p)

    # AC evidence: strip non-impl paths; promote evidenced impl files into modify.
    modify_paths = {f.get("path") for f in modify}
    for m in req_map.get("acceptanceMappings") or []:
        ev = [p for p in (m.get("graphEvidence") or []) if is_implementation_path(p)]
        m["graphEvidence"] = ev
        for path in ev:
            if path not in modify_paths:
                modify.append(
                    {
                        "path": path,
                        "purpose": f"AC evidence for {m.get('acId', 'AC')}",
                        "source": "ac-graph-evidence",
                    }
                )
                modify_paths.add(path)
        if ev:
            m["strategy"] = f"Modify evidenced files: {', '.join(ev)}"
        elif not (m.get("strategy") or "").startswith("No graph"):
            # Keep mediated strategies; otherwise mark for plan refinement.
            if "package.json" in (m.get("strategy") or "") or "project.json" in (m.get("strategy") or ""):
                m["strategy"] = (
                    "Use nearest implementation evidence; "
                    "config files excluded automatically."
                )

    # Also promote reuse impl files that look like app shells (App.tsx) when modify is thin.
    for r in reuse:
        path = r.get("path") or ""
        name = PurePosixPath(path.replace("\\", "/")).name.lower()
        if path and path not in modify_paths and name in {
            "app.tsx", "app.jsx", "app.vue", "app.svelte",
            "app.component.ts", "app.module.ts",
            "main.py", "app.py", "main.go", "main.rs",
            "application.java", "program.cs",
            "styles.css", "index.html", "index.ts", "index.js",
        }:
            modify.append(
                {
                    "path": path,
                    "purpose": "App shell / style entry promoted from reuse",
                    "source": "reuse-promote",
                }
            )
            modify_paths.add(path)

    req_map["create"] = create
    req_map["modify"] = modify
    req_map["reuse"] = reuse
    req_map["executionOrder"] = [f["path"] for f in modify] + [f["path"] for f in create]

    assumptions = list(req_map.get("assumptions") or [])
    if dropped:
        note = (
            "Auto-excluded non-implementation graph hits from edit targets: "
            + ", ".join(sorted(set(dropped)))
        )
        if note not in assumptions:
            assumptions.append(note)
    req_map["assumptions"] = assumptions

    stats = dict(req_map.get("stats") or {})
    stats["createCount"] = len(create)
    stats["modifyCount"] = len(modify)
    stats["reuseCount"] = len(reuse)
    stats["droppedNonImplCount"] = len(set(dropped))
    req_map["stats"] = stats
    req_map["sanitizedTargets"] = True
    return req_map


def sanitize_plan_targets(plan: dict[str, Any]) -> dict[str, Any]:
    create = [f for f in (plan.get("create") or []) if is_implementation_path(f.get("path"))]
    modify = [f for f in (plan.get("modify") or []) if is_implementation_path(f.get("path"))]
    plan["create"] = create
    plan["modify"] = modify
    order = [p for p in (plan.get("executionOrder") or []) if is_implementation_path(p)]
    if not order:
        order = [f["path"] for f in modify] + [f["path"] for f in create]
    plan["executionOrder"] = order
    return plan
