"""Stage runners.

Phase 0 provides deterministic stage effects and clearly-marked stubs for the
LLM stages (image convert, normalize, understanding, plan, review, implement).
Later phases replace the stubs with real Graphify queries and LLM calls, but the
control flow and gating are already authoritative here.
"""

from uiforgemax.stages.runner import StageResult, run_stage

__all__ = ["StageResult", "run_stage"]
