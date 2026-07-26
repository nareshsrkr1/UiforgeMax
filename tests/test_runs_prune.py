"""Retention prune for runs / _archive — sync logic + schedule single-flight."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uiforgemax.pipeline.runs_prune import (
    prune_old_runs,
    run_dir_age_mtime,
    runs_retention_days,
    schedule_prune_old_runs,
)


def _write_run(path: Path, *, updated_at: datetime, run_id: str | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    rid = run_id or path.name
    (path / "run.json").write_text(
        json.dumps(
            {
                "run_id": rid,
                "status": "completed",
                "updated_at": updated_at.isoformat(),
                "created_at": updated_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def test_runs_retention_days_unset_defaults_to_14(monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_RUNS_RETENTION_DAYS", raising=False)
    assert runs_retention_days() == 14


def test_runs_retention_days_parses(monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_RUNS_RETENTION_DAYS", "30")
    assert runs_retention_days() == 30
    monkeypatch.setenv("UIFORGEMAX_RUNS_RETENTION_DAYS", "0")
    assert runs_retention_days() == 0


def test_runs_retention_days_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_RUNS_RETENTION_DAYS", "nope")
    assert runs_retention_days() == 14


def test_prune_deletes_old_keeps_recent_and_keep_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_RUNS_RETENTION_DAYS", "14")
    runs = tmp_path / "runs"
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=20)
    recent = now - timedelta(days=2)

    _write_run(runs / "20260101-old", updated_at=old)
    _write_run(runs / "20260701-keepme", updated_at=old, run_id="20260701-keepme")
    _write_run(runs / "20260720-recent", updated_at=recent)
    _write_run(runs / "_archive" / "20251201-archived", updated_at=old)

    summary = prune_old_runs(
        runs,
        keep_run_ids={"20260701-keepme"},
        retention_days=14,
        max_dirs=40,
        max_seconds=30,
        now=now,
    )
    assert (runs / "20260101-old").exists() is False
    assert (runs / "_archive" / "20251201-archived").exists() is False
    assert (runs / "20260701-keepme").exists() is True
    assert (runs / "20260720-recent").exists() is True
    assert len(summary["pruned"]) == 2
    assert (runs / "_prune.log").exists()


def test_prune_disabled_when_zero(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_RUNS_RETENTION_DAYS", "0")
    runs = tmp_path / "runs"
    old = datetime.now(timezone.utc) - timedelta(days=40)
    _write_run(runs / "old-run", updated_at=old)
    summary = prune_old_runs(runs, retention_days=0)
    assert summary["enabled"] is False
    assert (runs / "old-run").exists()


def test_run_dir_age_prefers_updated_at(tmp_path: Path):
    path = tmp_path / "r1"
    ts = datetime(2020, 1, 1, tzinfo=timezone.utc)
    _write_run(path, updated_at=ts)
    assert run_dir_age_mtime(path).year == 2020


def test_schedule_prune_single_flight(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_RUNS_RETENTION_DAYS", "14")
    runs = tmp_path / "runs"
    runs.mkdir()
    # Force in-flight by holding the lock path: start one slow prune via monkeypatch
    import uiforgemax.pipeline.runs_prune as mod

    started = threading_event = __import__("threading").Event()
    release = __import__("threading").Event()

    def slow_prune(*_a, **_k):
        started.set()
        release.wait(timeout=2)
        return {"ok": True, "pruned": [], "skipped": [], "errors": [], "examined": 0, "enabled": True}

    monkeypatch.setattr(mod, "prune_old_runs", slow_prune)
    assert schedule_prune_old_runs(runs, keep_run_ids={"x"}, retention_days=14) is True
    assert started.wait(timeout=2)
    assert schedule_prune_old_runs(runs, keep_run_ids={"x"}, retention_days=14) is False
    release.set()
    time.sleep(0.1)  # allow worker to clear in-flight
