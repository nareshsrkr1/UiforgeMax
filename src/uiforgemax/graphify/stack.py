"""Cheap filesystem stack detection — before real Graphify runs.

Detects all major stacks (JS/TS, Python, Java, Go, .NET, Rust, Ruby, PHP,
Angular, Vue, Svelte, etc.) by file markers. Stack is informational — never
a hard gate. The pipeline works the same regardless of detected stack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_IGNORED = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "graphify-out", ".uiforgemax"}


def detect_stack(project_root: Path) -> dict[str, Any]:
    root = Path(project_root)
    empty = _is_empty(root)

    nx = _has_nx_marker(root)

    react = (
        any(root.rglob("*.tsx"))
        or any(root.rglob("*.jsx"))
        or _has_text_marker(root, (".html", ".tsx", ".jsx", ".ts", ".js"), ("react", "ReactDOM", 'from "react"'))
    )
    angular = (
        (root / "angular.json").exists()
        or _has_text_marker(root, (".ts",), ("@angular/core", "@Component", "@NgModule"))
    )
    vue = (
        any(root.rglob("*.vue"))
        or (root / "nuxt.config.ts").exists()
        or (root / "nuxt.config.js").exists()
        or _has_text_marker(root, (".ts", ".js"), ('from "vue"', "from 'vue'", "createApp"))
    )
    svelte = any(root.rglob("*.svelte")) or (root / "svelte.config.js").exists()

    fastapi = _has_text_marker(root, (".py",), ("FastAPI", "from fastapi", "import fastapi"))
    django = _has_text_marker(root, (".py",), ("from django", "import django", "DJANGO_SETTINGS_MODULE"))
    flask = _has_text_marker(root, (".py",), ("from flask", "import flask", "Flask(__name__"))
    express = _has_text_marker(root, (".ts", ".js"), ("express()", 'from "express"', "from 'express'"))
    spring = _has_text_marker(root, (".java", ".kt"), ("@SpringBootApplication", "org.springframework"))
    dotnet = (root / "*.csproj").exists() or any(root.rglob("*.csproj")) or any(root.rglob("*.sln"))
    golang = (root / "go.mod").exists()
    rust = (root / "Cargo.toml").exists()
    ruby = (root / "Gemfile").exists() or _has_text_marker(root, (".rb",), ("Rails.application", "class ApplicationController"))
    php = _has_text_marker(root, (".php",), ("<?php", "namespace App"))
    openfin = _has_text_marker(root, (".json", ".ts", ".js"), ("openfin", "fin.desktop", "fin.Platform"))

    kinds: list[str] = []
    for name, detected in [
        ("nx", nx), ("react", react), ("angular", angular), ("vue", vue),
        ("svelte", svelte), ("fastapi", fastapi), ("django", django),
        ("flask", flask), ("express", express), ("spring", spring),
        ("dotnet", dotnet), ("golang", golang), ("rust", rust),
        ("ruby", ruby), ("php", php), ("openfin", openfin),
    ]:
        if detected:
            kinds.append(name)

    if empty:
        primary = "greenfield"
    elif nx:
        primary = "nx"
    elif len(kinds) >= 2:
        primary = "-".join(kinds[:2])
    elif kinds:
        primary = kinds[0]
    else:
        primary = "standard"

    return {
        "primary": primary,
        "kinds": kinds,
        "nx": nx,
        "react": react,
        "angular": angular,
        "vue": vue,
        "svelte": svelte,
        "fastapi": fastapi,
        "django": django,
        "flask": flask,
        "express": express,
        "spring": spring,
        "dotnet": dotnet,
        "golang": golang,
        "rust": rust,
        "ruby": ruby,
        "php": php,
        "openfin": openfin,
        "empty": empty,
        "path": str(root.resolve()),
    }


def _has_nx_marker(root: Path, *, max_up: int = 4) -> bool:
    cur = root.resolve()
    for _ in range(max_up + 1):
        if (cur / "nx.json").exists() or (cur / "workspace.json").exists():
            return True
        if cur.parent == cur:
            break
        cur = cur.parent
    return False


def _is_empty(root: Path) -> bool:
    try:
        return not any(root.iterdir())
    except OSError:
        return True


def _has_text_marker(root: Path, suffixes: tuple[str, ...], markers: tuple[str, ...], limit_files: int = 80) -> bool:
    checked = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _IGNORED for part in path.parts):
            continue
        if path.suffix.lower() not in suffixes:
            continue
        checked += 1
        if checked > limit_files:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(m in text for m in markers):
            return True
    return False
