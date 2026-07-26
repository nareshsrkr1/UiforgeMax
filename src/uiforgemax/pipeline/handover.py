"""Handover report generation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_handover(
    run_dir: Path,
    diff_summary: dict[str, Any],
    test_results: dict[str, Any],
) -> Path:
    mode = diff_summary.get("mode") or "implement"
    md = f"""# UiForgeMax Delivery Report

## Summary
Implementation completed (`mode={mode}`). MCP does not create git branches —
review and commit in your workspace when ready.

## Files changed ({diff_summary.get('fileCount', 0)})
{chr(10).join('- ' + f for f in diff_summary.get('filesChanged', []))}

## Tests
Passed: **{test_results.get('passed')}**
Attempts: {len(test_results.get('attempts', []))}

## Next steps
1. Review `git status` / diff in the workspace
2. Run the full test suite locally for this stack
3. Commit / merge when satisfied
"""
    out = run_dir / "handover" / "delivery-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    (run_dir / "handover" / "delivery-report.json").write_text(
        json.dumps({"diff": diff_summary, "tests": test_results}, indent=2),
        encoding="utf-8",
    )
    return out
