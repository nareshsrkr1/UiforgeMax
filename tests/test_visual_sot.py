"""Visual SoT detection, HTML extract path, and fidelity-stage activation."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.model_mediation.registry import (
    MediationKind,
    build_mediation_request,
    pending_mediations,
)
from uiforgemax.pipeline.flow_router import build_flow_plan, is_stage_active
from uiforgemax.pipeline.normalize import _extract_html_structure, build_visual_spec
from uiforgemax.pipeline.testing import validate_test_generation
from uiforgemax.pipeline.visual_sot import detect_visual_references, resolve_run_path
from uiforgemax.pipeline.visual_validate import should_run_visual_validation
from uiforgemax.state import Stage, Status


def _state(modes: list[str] | None = None):
    return type(
        "S",
        (),
        {
            "inputs": {"modes": modes or []},
            "architecture": {},
            "flow": {},
            "status": Status.NORMALIZED,
        },
    )()


def test_resolve_run_path_relative(tmp_path: Path):
    html = tmp_path / "inputs" / "page.html"
    html.parent.mkdir()
    html.write_text("<html><body><h1>Hi</h1></body></html>", encoding="utf-8")
    assert resolve_run_path(tmp_path, "inputs/page.html") == html
    assert resolve_run_path(tmp_path, "inputs/missing.html") is None


def test_detect_visual_ref_html_only(tmp_path: Path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.html").write_text(
        "<html><nav></nav><main><button>Save</button></main></html>",
        encoding="utf-8",
    )
    (tmp_path / "inputs" / "attachments.json").write_text(
        json.dumps({"attachments": [{"path": "inputs/page.html", "role": "html", "filename": "page.html"}]}),
        encoding="utf-8",
    )
    ref = detect_visual_references(tmp_path, _state(["html"]))
    assert ref["hasHtml"] is True
    assert ref["hasVisualRef"] is True
    assert "html" in ref["kinds"]


def test_extract_html_structure_uses_run_dir(tmp_path: Path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.html").write_text(
        "<html><h1>Orders</h1><button>Create</button><nav></nav><main></main></html>",
        encoding="utf-8",
    )
    # Relative path must resolve against run_dir, not CWD.
    summary = _extract_html_structure("inputs/page.html", run_dir=tmp_path)
    labels = {c.get("label") or c.get("text") for c in summary["components"]}
    assert "Orders" in labels
    assert "Create" in labels
    region_ids = {r["id"] for r in summary["layout"]["regions"]}
    assert "nav" in region_ids
    assert "main" in region_ids


def test_build_visual_spec_html_derived(tmp_path: Path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.html").write_text(
        "<html><button>Approve</button></html>", encoding="utf-8"
    )
    spec = build_visual_spec(
        "inputs/page.html",
        sot={"primaryHtml": "inputs/page.html", "attachments": [], "designNotes": []},
        has_image=False,
        run_dir=tmp_path,
    )
    assert spec.get("htmlDerived") is True
    assert any(c.get("label") == "Approve" for c in spec.get("components") or [])


def test_flow_plan_html_activates_visual_validate(tmp_path: Path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.html").write_text("<html><body>x</body></html>", encoding="utf-8")
    flow = build_flow_plan(
        {"requestType": "enhancement", "surface": "ui_only", "runVisual": True},
        {"inputModes": ["html"], "projectRoot": {"empty": False}},
        _state(["html"]),
        run_dir=tmp_path,
    )
    assert flow["flags"]["hasVisualRef"] is True
    assert is_stage_active(Stage.VISUAL_VALIDATE, flow)


def test_should_run_visual_validation_html(tmp_path: Path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.html").write_text("<html></html>", encoding="utf-8")
    assert should_run_visual_validation(tmp_path, _state(["html"])) is True


def test_pending_visual_mediation_for_html(tmp_path: Path):
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "page.html").write_text("<html><button>Go</button></html>", encoding="utf-8")
    (tmp_path / "run-flow.json").write_text(
        json.dumps({"flags": {"runVisual": True, "hasVisualRef": True}, "activeStages": []}),
        encoding="utf-8",
    )
    state = _state(["html"])
    pending = pending_mediations(Stage.NORMALIZE, tmp_path, state)
    kinds = [k for _, k in pending]
    assert MediationKind.VISUAL_INTERPRETATION in kinds

    pending_v = pending_mediations(Stage.VISUAL_VALIDATE, tmp_path, state)
    assert MediationKind.VISUAL_VALIDATION in [k for _, k in pending_v]


def test_visual_validation_request_includes_html(tmp_path: Path):
    req = build_mediation_request(
        Stage.VISUAL_VALIDATE, MediationKind.VISUAL_VALIDATION, tmp_path, _state()
    )
    assert "inputs/page.html" in req["readArtifacts"]
    assert "sotKindsChecked" in req["outputSchema"]


def test_validate_test_generation_rejects_empty_ui(tmp_path: Path):
    (tmp_path / "request-classification.json").write_text(
        json.dumps({"surface": "ui_only"}), encoding="utf-8"
    )
    ok, reason = validate_test_generation(tmp_path, {"tests": [], "run": []})
    assert ok is False
    assert "empty" in reason.lower()


def test_validate_test_generation_accepts_real_tests(tmp_path: Path):
    (tmp_path / "request-classification.json").write_text(
        json.dumps({"surface": "ui_only"}), encoding="utf-8"
    )
    ok, reason = validate_test_generation(
        tmp_path,
        {
            "tests": [
                {
                    "path": "src/Foo.test.tsx",
                    "content": (
                        "import { render, screen } from '@testing-library/react';\n"
                        "import { Foo } from './Foo';\n"
                        "it('shows Save', () => {\n"
                        "  render(<Foo />);\n"
                        "  expect(screen.getByText('Save')).toBeTruthy();\n"
                        "});\n"
                    ),
                    "type": "dom",
                }
            ],
            "run": [{"command": "npx vitest run src/Foo.test.tsx", "cwd": ".", "root": "default"}],
        },
    )
    assert ok is True, reason
