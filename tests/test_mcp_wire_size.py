"""MCP tool JSON must stay lean so IDE hosts do not spill to content.json."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.mcp_response import tool_response
from uiforgemax.model_mediation.registry import attach_artifact_contents, wire_model_mediation
from uiforgemax.state import RunState, Status


def test_tool_response_is_compact_json():
    state = RunState(run_id="r1", status=Status.INTAKE)
    raw = tool_response(state, "hello", extra={"runsDir": "C:/tmp/runs/r1"})
    assert "\n  " not in raw  # no indent=2 pretty print
    body = json.loads(raw)
    assert body["runId"] == "r1"
    assert body["nextTool"]


def test_wire_mediation_skips_embeds_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_INLINE_ARTIFACTS", raising=False)
    run_dir = tmp_path / "run"
    (run_dir / "mediation").mkdir(parents=True)
    (run_dir / "plans").mkdir()
    (run_dir / "plans" / "tiny.json").write_text('{"a":1}', encoding="utf-8")
    req = {
        "mediationKey": "2_normalize::REQUIREMENT_ANALYSIS",
        "kind": "REQUIREMENT_ANALYSIS",
        "stage": "2_normalize",
        "instruction": "x" * 100,
        "readArtifacts": ["plans/tiny.json"],
        "outputSchema": {"summary": "string"},
    }
    (run_dir / "mediation" / "2_normalize_REQUIREMENT_ANALYSIS.request.json").write_text(
        json.dumps(req), encoding="utf-8"
    )
    wire = wire_model_mediation(req, run_dir)
    assert wire is not None
    assert "artifactContents" not in wire
    assert wire["requestFile"].endswith(".request.json")
    assert "get_run_status" in (wire.get("recovery") or "")


def test_tool_response_trims_over_budget():
    state = RunState(run_id="r1", status=Status.AWAITING_MEDIATION)
    huge = {"blob": "y" * 80_000}
    raw = tool_response(
        state,
        "x",
        stop=True,
        extra={
            "modelMediation": huge,
            "mediationBrief": {
                "kind": "X",
                "mediationKey": "k",
                "submitTool": "uiforgemax_submit_mediation",
                "instructionPreview": "z" * 500,
            },
            "runsDir": "/r",
        },
    )
    body = json.loads(raw)
    assert "modelMediation" not in body
    assert body.get("wireTrimmed") is True
    assert body["mediationBrief"]["kind"] == "X"
    assert "instructionPreview" not in body["mediationBrief"]
    assert body["nextTool"] == "uiforgemax_submit_mediation"


def test_inline_artifacts_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_INLINE_ARTIFACTS", "1")
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    (run_dir / "plans" / "tiny.json").write_text('{"a":1}', encoding="utf-8")
    req = {"readArtifacts": ["plans/tiny.json"]}
    out = attach_artifact_contents(req, run_dir)
    assert out["artifactContents"]["plans/tiny.json"] == '{"a":1}'
