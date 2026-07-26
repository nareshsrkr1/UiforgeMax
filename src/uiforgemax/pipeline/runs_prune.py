"""Best-effort retention prune for ``<runs_root>/`` and ``_archive/``.

Triggered asynchronously from ``start_run`` when ``UIFORGEMAX_RUNS_RETENTION_DAYS``
is set (>0). Never raises into the pipeline — errors append to ``_prune.log``.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_IN_FLIGHT = False

# Soft caps so a hung/huge tree cannot monopolize the MCP process.
_DEFAULT_MAX_DIRS = 40
_DEFAULT_MAX_SECONDS = 8.0
# Never delete anything touched this recently (active start / race guard).
_MIN_AGE_MINUTES = 30


def runs_retention_days(default: int = 14) -> int:
    """Days to keep run dirs under ``UIFORGEMAX_RUNS_ROOT``.

    Default **14** when ``UIFORGEMAX_RUNS_RETENTION_DAYS`` is unset/empty.
    Set the env to ``0`` to disable prune. Invalid values fall back to ``default``.
    """
    import os

    raw = os.getenv("UIFORGEMAX_RUNS_RETENTION_DAYS")
    if raw is None or not str(raw).strip():
        return max(0, int(default))
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return max(0, int(default))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def run_dir_age_mtime(path: Path) -> datetime:
    """Best-effort last activity time for a run (or archive) directory."""
    run_json = path / "run.json"
    if run_json.is_file():
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
            for key in ("updated_at", "created_at"):
                dt = _parse_ts(data.get(key) if isinstance(data, dict) else None)
                if dt is not None:
                    return dt
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def _append_log(runs_root: Path, line: str) -> None:
    try:
        runs_root.mkdir(parents=True, exist_ok=True)
        log_path = runs_root / "_prune.log"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except OSError:
        pass


def _candidate_dirs(runs_root: Path) -> list[Path]:
    out: list[Path] = []
    if not runs_root.is_dir():
        return out
    try:
        for p in runs_root.iterdir():
            if not p.is_dir():
                continue
            if p.name.startswith("."):
                continue
            if p.name == "_archive":
                try:
                    for archived in p.iterdir():
                        if archived.is_dir():
                            out.append(archived)
                except OSError:
                    pass
                continue
            out.append(p)
    except OSError:
        return out
    return out


def prune_old_runs(
    runs_root: str | Path,
    *,
    keep_run_ids: set[str] | None = None,
    retention_days: int | None = None,
    max_dirs: int = _DEFAULT_MAX_DIRS,
    max_seconds: float = _DEFAULT_MAX_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete run dirs older than ``retention_days``. Sync; safe to call from a thread.

    Returns a small summary dict (also appended to ``_prune.log``).
    """
    root = Path(runs_root)
    days = runs_retention_days() if retention_days is None else max(0, int(retention_days))
    summary: dict[str, Any] = {
        "ok": True,
        "enabled": days > 0,
        "retentionDays": days,
        "pruned": [],
        "skipped": [],
        "errors": [],
        "examined": 0,
    }
    if days <= 0:
        return summary

    keep = {str(x) for x in (keep_run_ids or set()) if x}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    floor = (now or datetime.now(timezone.utc)) - timedelta(minutes=_MIN_AGE_MINUTES)
    started = time.monotonic()
    deleted = 0

    for path in sorted(_candidate_dirs(root), key=lambda p: p.name):
        if deleted >= max_dirs or (time.monotonic() - started) >= max_seconds:
            summary["skipped"].append({"path": str(path), "reason": "budget"})
            break
        summary["examined"] += 1
        name = path.name
        if name in keep:
            summary["skipped"].append({"path": str(path), "reason": "keep_run_id"})
            continue
        # Also protect keep ids when under _archive/<id>
        if path.parent.name == "_archive" and name in keep:
            summary["skipped"].append({"path": str(path), "reason": "keep_run_id"})
            continue
        age = run_dir_age_mtime(path)
        if age > floor:
            summary["skipped"].append({"path": str(path), "reason": "too_recent"})
            continue
        if age > cutoff:
            summary["skipped"].append({"path": str(path), "reason": "within_retention"})
            continue
        try:
            shutil.rmtree(path, ignore_errors=False)
            summary["pruned"].append(str(path))
            deleted += 1
        except OSError as exc:
            summary["errors"].append({"path": str(path), "error": str(exc)[:200]})
            summary["ok"] = False

    _append_log(
        root,
        (
            f"retentionDays={days} examined={summary['examined']} "
            f"pruned={len(summary['pruned'])} skipped={len(summary['skipped'])} "
            f"errors={len(summary['errors'])}"
        ),
    )
    return summary


def schedule_prune_old_runs(
    runs_root: str | Path,
    *,
    keep_run_ids: set[str] | None = None,
    retention_days: int | None = None,
) -> bool:
    """Start a daemon thread prune if retention is enabled and none is in flight.

    Returns True if a thread was started. Failures never propagate.
    """
    global _IN_FLIGHT
    days = runs_retention_days() if retention_days is None else max(0, int(retention_days))
    if days <= 0:
        return False

    with _LOCK:
        if _IN_FLIGHT:
            _append_log(Path(runs_root), "skip: prune already in flight")
            return False
        _IN_FLIGHT = True

    root = Path(runs_root)
    keep = set(keep_run_ids or set())

    def _worker() -> None:
        global _IN_FLIGHT
        try:
            prune_old_runs(root, keep_run_ids=keep, retention_days=days)
        except Exception as exc:  # noqa: BLE001 — never break MCP
            _append_log(root, f"error: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            with _LOCK:
                _IN_FLIGHT = False

    try:
        threading.Thread(target=_worker, name="uiforgemax-runs-prune", daemon=True).start()
        return True
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _IN_FLIGHT = False
        _append_log(root, f"schedule_failed: {type(exc).__name__}: {str(exc)[:200]}")
        return False
