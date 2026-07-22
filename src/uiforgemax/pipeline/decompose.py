"""Sub-task decomposition — splits complex requirements into ordered, dependency-aware sub-tasks.

Simple requests (≤2 ACs) auto-wrap into a single ST-ALL sub-task with no mediation.
Complex requests pause for IDE TASK_DECOMPOSITION mediation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_VERSION = 1


def should_auto_decompose(requirements: dict[str, Any], classification: dict[str, Any]) -> bool:
    acs = requirements.get("acceptanceCriteria") or []
    return len(acs) <= 2


def auto_decompose(
    requirements: dict[str, Any],
    classification: dict[str, Any],
    visual_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    acs = requirements.get("acceptanceCriteria") or []
    ac_ids = [ac.get("id") or f"AC-{i+1}" for i, ac in enumerate(acs)]
    summary = requirements.get("summary") or requirements.get("title") or "Full request"
    surface = classification.get("surface") or "unknown"

    keywords: list[str] = []
    hints = requirements.get("graphSearchHints") or {}
    if hints.get("keywords"):
        keywords = list(hints["keywords"])
    if not keywords:
        keywords = _extract_keywords(summary)

    return {
        "version": _VERSION,
        "decompositionStrategy": "single_wrap",
        "subtaskCount": 1,
        "subtasks": [
            {
                "id": "ST-ALL",
                "title": summary[:120],
                "summary": summary,
                "surfaceHint": surface,
                "linkedAcIds": ac_ids,
                "dependencies": [],
                "visualRegion": None,
                "priority": 1,
                "order": 1,
                "focusKeywords": keywords[:8],
                "estimatedFiles": [],
                "complexity": "low",
            }
        ],
        "dependencyOrder": ["ST-ALL"],
        "parallelGroups": [["ST-ALL"]],
        "source": "auto",
        "mediatedByIde": False,
    }


def build_subtasks_from_mediation(
    requirements: dict[str, Any],
    classification: dict[str, Any],
    visual_spec: dict[str, Any] | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    raw_subtasks = payload.get("subtasks") or []
    if not raw_subtasks:
        return auto_decompose(requirements, classification, visual_spec)

    all_ac_ids = {
        ac.get("id") or f"AC-{i+1}"
        for i, ac in enumerate(requirements.get("acceptanceCriteria") or [])
    }

    subtasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    linked_acs: set[str] = set()

    for i, raw in enumerate(raw_subtasks):
        st_id = raw.get("id") or f"ST-{i + 1}"
        if st_id in seen_ids:
            st_id = f"{st_id}-{i}"
        seen_ids.add(st_id)

        st_acs = raw.get("linkedAcIds") or []
        linked_acs.update(st_acs)

        subtasks.append({
            "id": st_id,
            "title": raw.get("title") or f"Sub-task {i + 1}",
            "summary": raw.get("summary") or raw.get("title") or "",
            "surfaceHint": raw.get("surfaceHint") or classification.get("surface") or "unknown",
            "linkedAcIds": st_acs,
            "dependencies": raw.get("dependencies") or [],
            "visualRegion": raw.get("visualRegion"),
            "priority": raw.get("priority") or (i + 1),
            "order": raw.get("order") or (i + 1),
            "focusKeywords": (raw.get("focusKeywords") or [])[:10],
            "searchContext": raw.get("searchContext") or {},
            "estimatedFiles": raw.get("estimatedFiles") or [],
            "complexity": raw.get("complexity") or "medium",
        })

    orphan_acs = all_ac_ids - linked_acs
    if orphan_acs and subtasks:
        subtasks[-1]["linkedAcIds"] = list(
            set(subtasks[-1]["linkedAcIds"]) | orphan_acs
        )

    dep_order = _topological_sort(subtasks)
    parallel_groups = _compute_parallel_groups(subtasks, dep_order)

    return {
        "version": _VERSION,
        "decompositionStrategy": payload.get("decompositionStrategy") or "ac_grouping",
        "subtaskCount": len(subtasks),
        "subtasks": subtasks,
        "dependencyOrder": dep_order,
        "parallelGroups": parallel_groups,
        "source": "mediation",
        "mediatedByIde": True,
    }


def load_subtasks(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "plans" / "subtasks.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def subtask_ac_slice(
    subtask: dict[str, Any],
    requirements: dict[str, Any],
) -> dict[str, Any]:
    linked = set(subtask.get("linkedAcIds") or [])
    if not linked:
        return requirements

    filtered_acs = [
        ac for ac in (requirements.get("acceptanceCriteria") or [])
        if (ac.get("id") or "") in linked
    ]

    sliced = dict(requirements)
    sliced["acceptanceCriteria"] = filtered_acs
    sliced["summary"] = subtask.get("summary") or requirements.get("summary") or ""
    return sliced


def subtask_keywords(subtask: dict[str, Any]) -> list[str]:
    return list(subtask.get("focusKeywords") or [])


def _extract_keywords(text: str) -> list[str]:
    import re
    stop = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "must", "to", "of",
        "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
        "and", "or", "but", "not", "no", "if", "then", "than", "so", "that",
        "this", "it", "its", "all", "each", "every", "both", "few", "more",
        "when", "where", "how", "what", "which", "who", "whom", "why",
    }
    words = re.findall(r"[A-Za-z][a-z]{2,}", text)
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        lower = w.lower()
        if lower not in stop and lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result[:8]


def _topological_sort(subtasks: list[dict[str, Any]]) -> list[str]:
    id_set = {st["id"] for st in subtasks}
    graph: dict[str, list[str]] = {st["id"]: [] for st in subtasks}
    in_degree: dict[str, int] = {st["id"]: 0 for st in subtasks}

    for st in subtasks:
        for dep in st.get("dependencies") or []:
            if dep in id_set:
                graph[dep].append(st["id"])
                in_degree[st["id"]] += 1

    queue = sorted(
        [sid for sid, deg in in_degree.items() if deg == 0],
        key=lambda sid: next(
            (st.get("priority", 99) for st in subtasks if st["id"] == sid), 99
        ),
    )
    result: list[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for neighbor in graph.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
        queue.sort(
            key=lambda sid: next(
                (st.get("priority", 99) for st in subtasks if st["id"] == sid), 99
            ),
        )

    if len(result) < len(subtasks):
        missing = id_set - set(result)
        result.extend(sorted(missing))

    return result


def _compute_parallel_groups(
    subtasks: list[dict[str, Any]],
    dep_order: list[str],
) -> list[list[str]]:
    deps_by_id: dict[str, set[str]] = {}
    for st in subtasks:
        deps_by_id[st["id"]] = set(st.get("dependencies") or [])

    groups: list[list[str]] = []
    placed: set[str] = set()

    remaining = list(dep_order)
    while remaining:
        group: list[str] = []
        for sid in remaining:
            if deps_by_id.get(sid, set()).issubset(placed):
                group.append(sid)
        if not group:
            group = remaining[:]
        groups.append(group)
        placed.update(group)
        remaining = [s for s in remaining if s not in placed]

    return groups
