"""Deterministic file evidence from graph.json when NL ``graphify query`` times out.

NL queries on Windows often exceed 25s even on small graphs. Scanning node
``source_file`` / labels is enough for UI theming tickets and keeps the pipeline moving.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_STYLE_RE = re.compile(
    r"style|css|theme|dark|nav|sidebar|background|layout|app\.tsx|page",
    re.I,
)
_IMPL_SUFFIXES = (".tsx", ".ts", ".jsx", ".js", ".css", ".scss", ".html")


def lexical_evidence_from_graph(
    graph_path: Path,
    *,
    keywords: list[str] | None = None,
    limit: int = 24,
) -> list[dict[str, str]]:
    """Return NODE-like dicts ``{label, source_file, source_location}`` from graph.json."""
    try:
        data = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    needles = [k.lower() for k in (keywords or []) if k]
    nodes_out: list[dict[str, str]] = []
    seen: set[str] = set()

    for n in data.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        src = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not src or not src.lower().endswith(_IMPL_SUFFIXES):
            continue
        label = str(n.get("label") or n.get("id") or src)
        blob = f"{label} {src}".lower()
        score = 0
        if _STYLE_RE.search(blob):
            score += 2
        for k in needles:
            if k and k in blob:
                score += 3
        # Always keep app shell / stylesheet paths even without keyword hit.
        name = Path(src).name.lower()
        if name in {"app.tsx", "app.jsx", "styles.css", "index.css", "global.css", "index.html"}:
            score += 4
        if "sidebar" in blob or "nav" in blob:
            score += 2
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
    if not requirements:
        return ["dark", "nav", "sidebar", "background", "style", "theme"]
    parts = [requirements.get("summary") or ""]
    for ac in requirements.get("acceptanceCriteria") or []:
        if isinstance(ac, dict):
            parts.append(ac.get("text") or "")
        else:
            parts.append(str(ac))
    blob = " ".join(parts).lower()
    base = ["dark", "nav", "sidebar", "background", "style", "theme", "css"]
    extra = [w for w in ("layout", "page", "chrome", "token") if w in blob]
    return base + extra


_STYLE_CANDIDATES = (
    "src/styles.css",
    "src/index.css",
    "src/App.css",
    "src/theme.css",
    "styles.css",
    "index.css",
)


def supplement_style_files(
    project_root: Path | None,
    nodes: list[dict[str, str]],
    *,
    allow_file_discovery: bool = False,
) -> list[dict[str, str]]:
    """Optional on-disk stylesheet probe — OFF unless file discovery is explicitly allowed.

    Default path is graph-only (``lexical_evidence_from_graph``). File discovery is for
    when graph queries fail and mediation opts in (``UIFORGEMAX_ALLOW_FILE_DISCOVERY=1``).
    """
    if not allow_file_discovery or project_root is None:
        return nodes
    root = Path(project_root)
    have = {n.get("source_file", "").replace("\\", "/") for n in nodes}
    out = list(nodes)
    for rel in _STYLE_CANDIDATES:
        if rel in have:
            continue
        if (root / rel).is_file():
            out.append(
                {
                    "label": rel,
                    "source_file": rel,
                    "source_location": "fs-style-supplement",
                }
            )
            have.add(rel)
    return out
