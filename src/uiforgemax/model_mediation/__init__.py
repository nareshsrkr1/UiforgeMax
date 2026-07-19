"""IDE model mediation — Cursor/VS Code interprets; MCP merges structured JSON."""

from uiforgemax.model_mediation.registry import (
    MEDIATION_BY_STAGE,
    MediationKind,
    build_mediation_request,
)
from uiforgemax.model_mediation.service import (
    apply_mediation,
    clear_mediation_responses,
    is_mediation_complete,
    is_mediation_skipped,
    load_mediation_request,
    mediation_dir,
    mediation_pause_result,
    save_mediation_request,
)

__all__ = [
    "MEDIATION_BY_STAGE",
    "MediationKind",
    "apply_mediation",
    "build_mediation_request",
    "clear_mediation_responses",
    "is_mediation_complete",
    "is_mediation_skipped",
    "load_mediation_request",
    "mediation_dir",
    "mediation_pause_result",
    "save_mediation_request",
]
