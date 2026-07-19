"""The run state machine and its persistence.

Canonical stages and statuses mirror ``MASTER_PLAN.md``. The ordering here is
authoritative: :func:`RunState.advance_target` uses it to compute the next
stage, and the gating engine uses recorded approvals to permit or refuse work.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Stage(str, Enum):
    """Canonical pipeline stages (see MASTER_PLAN §2). Gate stages (.5) are
    represented as their own steps so the state machine can pause on them."""

    INTAKE = "0_intake"
    ARCH_DETECT = "0.5_arch_detect"
    CLASSIFY = "0.6_classify"
    IMAGE_CONVERT = "1_image_convert"
    NORMALIZE = "2_normalize"
    GRAPHIFY_UPDATE = "3_graphify_update"
    GRAPH_MERGE = "3.5_graph_merge"
    GRAPH_QUERY_PLAN = "4_graph_query_plan"
    GRAPH_QUERY_EXEC = "4.5_graph_query_exec"
    REQUIREMENT_MAP = "4.6_requirement_map"
    API_RESOLVE = "5_api_resolve"
    GATE_API = "5.5_gate_api"
    UNDERSTANDING = "6_understanding"
    GATE_UNDERSTANDING = "6.5_gate_understanding"
    PLAN = "7_plan"
    PLAN_REVIEW = "8_plan_review"
    GATE_PLAN = "8.5_gate_plan"
    IMPLEMENT = "9_implement"
    TEST = "10_test"
    HANDOVER = "11_handover"

    @classmethod
    def ordered(cls) -> list["Stage"]:
        return [
            cls.INTAKE,
            cls.ARCH_DETECT,
            cls.CLASSIFY,
            cls.IMAGE_CONVERT,
            cls.NORMALIZE,
            cls.GRAPHIFY_UPDATE,
            cls.GRAPH_MERGE,
            cls.GRAPH_QUERY_PLAN,
            cls.GRAPH_QUERY_EXEC,
            cls.REQUIREMENT_MAP,
            cls.API_RESOLVE,
            cls.GATE_API,
            cls.UNDERSTANDING,
            cls.GATE_UNDERSTANDING,
            cls.PLAN,
            cls.PLAN_REVIEW,
            cls.GATE_PLAN,
            cls.IMPLEMENT,
            cls.TEST,
            cls.HANDOVER,
        ]

    def next(self) -> "Stage | None":
        order = self.ordered()
        idx = order.index(self)
        return order[idx + 1] if idx + 1 < len(order) else None


class Status(str, Enum):
    INTAKE = "intake"
    ARCH_DETECTED = "arch_detected"
    CLASSIFIED = "classified"
    IMAGE_CONVERTED = "image_converted"
    NORMALIZED = "normalized"
    GRAPH_READY = "graph_ready"
    GRAPH_MERGED = "graph_merged"
    GRAPH_QUERIED = "graph_queried"
    REQUIREMENT_MAPPED = "requirement_mapped"
    API_RESOLVED = "api_resolved"
    AWAITING_MEDIATION = "awaiting_mediation"
    AWAITING_API_APPROVAL = "awaiting_api_approval"
    UNDERSTANDING_READY = "understanding_ready"
    AWAITING_UNDERSTANDING_APPROVAL = "awaiting_understanding_approval"
    PLAN_READY = "plan_ready"
    PLAN_REVIEWED = "plan_reviewed"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    AWAITING_USER_INSTALL = "awaiting_user_install"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    @classmethod
    def terminal(cls) -> set["Status"]:
        return {cls.COMPLETED, cls.FAILED, cls.CANCELLED}


class Approval(BaseModel):
    required: bool = False
    approved: bool = False
    at: str | None = None
    by: str | None = None
    feedback: str | None = None


class Approvals(BaseModel):
    api: Approval = Field(default_factory=Approval)
    # Understanding is an artifact only — sole human gate before implement is plan.
    understanding: Approval = Field(default_factory=lambda: Approval(required=False))
    plan: Approval = Field(default_factory=lambda: Approval(required=True))


class HistoryEntry(BaseModel):
    stage: str
    at: str = Field(default_factory=_now)
    result: str
    detail: str | None = None


class Compliance(BaseModel):
    match_exactly: bool = False
    sources: list[str] = Field(default_factory=list)


class RunState(BaseModel):
    run_id: str
    project_root: str | None = None
    # Additional named repo roots beyond the primary `project_root`, keyed by
    # a name a plan action can reference via its own `"root"` field (e.g. a
    # plan touching two unrelated repos, not just subfolders of one). Added
    # via `uiforgemax_add_workspace_root`; the PLAN stage blocks and asks for
    # any root a plan references that isn't registered here yet.
    project_roots: dict[str, str] = Field(default_factory=dict)
    status: Status = Status.INTAKE
    current_stage: Stage = Stage.INTAKE
    inputs: dict = Field(default_factory=lambda: {"modes": []})
    architecture: dict = Field(default_factory=dict)
    compliance: Compliance = Field(default_factory=Compliance)
    policy: str = "pending"
    flow: dict = Field(default_factory=dict)
    artifacts: dict = Field(default_factory=dict)
    approvals: Approvals = Field(default_factory=Approvals)
    mediation: dict = Field(default_factory=dict)
    history: list[HistoryEntry] = Field(default_factory=list)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    def record(self, stage: Stage, result: str, detail: str | None = None) -> None:
        self.history.append(HistoryEntry(stage=stage.value, result=result, detail=detail))
        self.updated_at = _now()

    def add_mode(self, mode: str) -> None:
        modes = self.inputs.setdefault("modes", [])
        if mode not in modes:
            modes.append(mode)


class RunStore:
    """Loads and persists ``run.json`` under ``<runs_root>/<run_id>/``."""

    def __init__(self, runs_root: str | Path):
        self.runs_root = Path(runs_root)

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id

    def run_file(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.json"

    def exists(self, run_id: str) -> bool:
        return self.run_file(run_id).exists()

    def create(
        self,
        run_id: str,
        project_root: str | None,
        policy: str,
        project_roots: dict[str, str] | None = None,
    ) -> RunState:
        if self.exists(run_id):
            raise ValueError(f"run '{run_id}' already exists")
        state = RunState(
            run_id=run_id,
            project_root=project_root,
            policy=policy,
            project_roots=dict(project_roots or {}),
        )
        state.record(Stage.INTAKE, "created")
        self._ensure_dirs(run_id)
        self.save(state)
        return state

    def load(self, run_id: str) -> RunState:
        path = self.run_file(run_id)
        if not path.exists():
            raise FileNotFoundError(f"run '{run_id}' not found at {path}")
        return RunState.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, state: RunState) -> None:
        state.updated_at = _now()
        path = self.run_file(state.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def list_runs(self) -> list[str]:
        if not self.runs_root.exists():
            return []
        return sorted(
            p.name
            for p in self.runs_root.iterdir()
            if p.is_dir() and p.name != "_archive" and (p / "run.json").exists()
        )

    def archive_run(self, run_id: str, reason: str | None = None) -> Path | None:
        """Move a run dir under ``_archive/`` so a fresh start does not resume it."""
        import shutil

        src = self.run_dir(run_id)
        if not src.exists():
            return None
        # Best-effort: mark cancelled before moving.
        try:
            if self.exists(run_id):
                state = self.load(run_id)
                if state.status not in Status.terminal():
                    state.status = Status.CANCELLED
                    state.record(state.current_stage, "archived", reason or "superseded by start")
                    self.save(state)
        except Exception:  # noqa: BLE001
            pass
        dest_root = self.runs_root / "_archive"
        dest_root.mkdir(parents=True, exist_ok=True)
        dest = dest_root / run_id
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(src), str(dest))
        return dest

    def _ensure_dirs(self, run_id: str) -> None:
        base = self.run_dir(run_id)
        for sub in ("inputs", "graph", "plans", "implementation", "tests", "handover", "mediation"):
            (base / sub).mkdir(parents=True, exist_ok=True)
