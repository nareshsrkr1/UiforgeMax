"""Per-run stage statistics."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def record_stage(run_dir: Path, stage: str, stats: dict[str, Any], duration_ms: float) -> None:
    path = run_dir / "stats.json"
    data: dict[str, Any] = {"stages": []}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["stages"].append(
        {
            "stage": stage,
            "durationMs": round(duration_ms, 2),
            **stats,
        }
    )
    data["totals"] = {
        "stageCount": len(data["stages"]),
        "totalDurationMs": round(sum(s["durationMs"] for s in data["stages"]), 2),
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


class StageTimer:
    def __init__(self, run_dir: Path, stage: str):
        self.run_dir = run_dir
        self.stage = stage
        self._start = 0.0
        self.stats: dict[str, Any] = {}

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *_):
        duration = (time.perf_counter() - self._start) * 1000
        record_stage(self.run_dir, self.stage, self.stats, duration)
