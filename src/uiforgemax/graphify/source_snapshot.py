"""Deterministic MCP-side snapshot of literal file content for plan mediation.

Graphify's graph.json is structural (imports, declarations, line hints) — it never
carries literal source text (no CSS selectors, no JSX body, no business logic).
Legacy helper (lean architecture no longer requires snapshots for PLAN_REFINEMENT).
Historically PLAN_REFINEMENT required full file `content` for every
`modify` action, but the driving agent is forbidden from reading target-project
files directly (that would defeat the graph-driven flow). MCP itself is already
allowed to touch target files (Graphify parses them, implement writes them) — so
MCP reads the *current* text of plan candidate files here and hands it back as a
run-dir artifact, which the driving agent MAY read. This lets mediation edit real
files without guessing/deleting code it can't see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 200_000
MAX_FILES = 60
_TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".css", ".scss", ".sass", ".less",
    ".html", ".htm", ".json", ".py", ".java", ".kt", ".go", ".cs",
    ".md", ".yml", ".yaml", ".vue", ".svelte", ".txt", ".xml",
    ".gradle", ".toml", ".cfg", ".ini",
}


def _candidate_paths(plan_like: dict[str, Any]) -> list[dict[str, str]]:
    """Distinct (root, path) pairs from modify/reuse/create — modify first (most needed)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for bucket in ("modify", "reuse", "create"):
        for entry in plan_like.get(bucket) or []:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path:
                continue
            root = entry.get("root") or "default"
            key = (root, path)
            if key in seen:
                continue
            seen.add(key)
            out.append({"path": path, "root": root, "bucket": bucket})
    return out


def build_source_snapshots(
    roots: dict[str, Path],
    plan_like: dict[str, Any],
) -> dict[str, Any]:
    """Read literal current content for modify/reuse/create candidates (MCP-only FS read).

    Caps: ``MAX_FILES`` entries, ``MAX_FILE_BYTES`` per file (larger files are
    truncated with a flag rather than dropped, so mediation still sees the head).
    """
    files: dict[str, Any] = {}
    candidates = _candidate_paths(plan_like)[:MAX_FILES]
    for cand in candidates:
        root_name = cand["root"]
        root = roots.get(root_name) or roots.get("default")
        key = f"{root_name}:{cand['path']}"
        if root is None:
            files[key] = {
                "path": cand["path"],
                "root": root_name,
                "bucket": cand["bucket"],
                "exists": False,
                "reason": f"unknown root '{root_name}'",
            }
            continue
        target = (root / cand["path"]).resolve()
        if not target.exists() or not target.is_file():
            files[key] = {
                "path": cand["path"],
                "root": root_name,
                "bucket": cand["bucket"],
                "exists": False,
            }
            continue
        suffix = target.suffix.lower()
        if suffix not in _TEXT_SUFFIXES:
            files[key] = {
                "path": cand["path"],
                "root": root_name,
                "bucket": cand["bucket"],
                "exists": True,
                "binary": True,
                "sizeBytes": target.stat().st_size,
            }
            continue
        try:
            raw = target.read_bytes()
        except OSError as exc:
            files[key] = {
                "path": cand["path"],
                "root": root_name,
                "bucket": cand["bucket"],
                "exists": True,
                "error": str(exc),
            }
            continue
        truncated = len(raw) > MAX_FILE_BYTES
        text = raw[:MAX_FILE_BYTES].decode("utf-8", errors="replace")
        files[key] = {
            "path": cand["path"],
            "root": root_name,
            "bucket": cand["bucket"],
            "exists": True,
            "content": text,
            "lineCount": text.count("\n") + 1,
            "truncated": truncated,
        }

    return {
        "engine": "mcp-fs-snapshot",
        "note": (
            "Literal current content for plan candidate files, read by MCP itself "
            "(deterministic FS read — not the driving IDE agent). For any 'modify' "
            "entry with exists=true, use this content as the base: apply only the "
            "requirement's change and return the FULL edited file so unrelated code/CSS "
            "rules are preserved. If exists=false (new file), binary=true, or an entry "
            "you need is missing/truncated, do not fabricate structure you can't see — "
            "prefer a minimal safe change, add a clarification, or ask for the file to "
            "be re-snapshotted rather than guessing."
        ),
        "maxFiles": MAX_FILES,
        "maxFileBytes": MAX_FILE_BYTES,
        "fileCount": len(files),
        "files": files,
    }
