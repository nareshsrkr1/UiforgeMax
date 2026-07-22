"""Deterministic file evidence from graph.json via keyword matching.

Stack-agnostic: uses mediated keywords from upstream mediations, not hardcoded
file patterns. Serves as fast deterministic evidence before any graphify query.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_IMPL_SUFFIXES = (
    ".tsx", ".ts", ".jsx", ".js", ".css", ".scss", ".less", ".html",
    ".py", ".go", ".rs", ".java", ".cs", ".rb", ".php", ".vue", ".svelte",
)

_CONFIG_FILES = {
    # JS/TS
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "tsconfig.json", "jsconfig.json", "project.json", "nx.json", "angular.json",
    "vite.config.ts", "vite.config.js", "webpack.config.js", "webpack.config.ts",
    "next.config.js", "next.config.mjs", "nuxt.config.ts", "nuxt.config.js",
    "jest.config.js", "jest.config.ts", "vitest.config.ts", "svelte.config.js",
    ".eslintrc.js", ".eslintrc.json", "eslint.config.js",
    "tailwind.config.js", "tailwind.config.ts", "postcss.config.js",
    # Python
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "poetry.lock",
    "pipfile", "pipfile.lock",
    # Java/Kotlin
    "pom.xml", "build.gradle", "build.gradle.kts",
    "settings.gradle", "settings.gradle.kts", "gradle.properties",
    # Go / Rust
    "cargo.toml", "cargo.lock", "go.mod", "go.sum",
    # .NET
    "nuget.config",
    # Ruby / PHP
    "gemfile", "gemfile.lock", "composer.json", "composer.lock",
    # General
    "readme.md", "readme.txt", "changelog.md", "license", "license.md",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "makefile", ".gitignore", ".editorconfig",
}


def lexical_evidence_from_graph(
    graph_path: Path,
    *,
    keywords: list[str] | None = None,
    limit: int = 24,
) -> list[dict[str, str]]:
    """Return NODE-like dicts from graph.json matched by keywords."""
    try:
        data = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    needles = [k.lower() for k in (keywords or []) if k]
    if not needles:
        return []

    nodes_out: list[dict[str, str]] = []
    seen: set[str] = set()

    for n in data.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        src = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not src or not src.lower().endswith(_IMPL_SUFFIXES):
            continue
        basename = Path(src).name.lower()
        if basename in _CONFIG_FILES:
            continue
        label = str(n.get("label") or n.get("id") or src)
        blob = f"{label} {src}".lower()
        score = 0
        for k in needles:
            if k and k in blob:
                score += 3
        if score <= 0:
            continue
        key = src.lower()
        if key in seen:
            continue
        seen.add(key)
        nodes_out.append(
            {
                "label": label[:120],
                "source_file": src,
                "source_location": str(n.get("source_location") or n.get("id") or ""),
                "_score": str(score),
            }
        )

    nodes_out.sort(key=lambda x: (-int(x.get("_score") or 0), x["source_file"]))
    cleaned: list[dict[str, str]] = []
    for n in nodes_out[:limit]:
        n.pop("_score", None)
        cleaned.append(n)
    return cleaned


def keywords_from_requirements(requirements: dict[str, Any] | None) -> list[str]:
    """Extract search keywords from requirements — stack-agnostic."""
    if not requirements:
        return []
    strategy = requirements.get("graphSearchStrategy")
    if strategy and strategy.get("keywords"):
        kws = list(strategy["keywords"])
        kws.extend(strategy.get("componentNames") or [])
        return [k.lower() for k in kws if k][:12]
    hints = requirements.get("graphSearchHints")
    if hints and hints.get("keywords"):
        return [k.lower() for k in hints["keywords"] if k][:12]
    parts = [requirements.get("summary") or ""]
    for ac in requirements.get("acceptanceCriteria") or []:
        if isinstance(ac, dict):
            parts.append(ac.get("text") or "")
        else:
            parts.append(str(ac))
    blob = " ".join(parts).lower()
    stop = {
        "the", "and", "for", "with", "this", "that", "from", "into",
        "should", "must", "will", "have", "been", "using", "uses",
        "when", "then", "also", "each", "every", "only", "both",
    }
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", blob)
    out: list[str] = []
    for t in tokens:
        t = t.lower()
        if t in stop or t in out:
            continue
        out.append(t)
        if len(out) >= 10:
            break
    return out


def supplement_style_files(
    project_root: Path | None,
    nodes: list[dict[str, str]],
    *,
    allow_file_discovery: bool = False,
) -> list[dict[str, str]]:
    """Optional on-disk file probe — OFF unless file discovery is explicitly allowed."""
    if not allow_file_discovery or project_root is None:
        return nodes
    return nodes
