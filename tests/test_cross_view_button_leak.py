"""A button that's textually correct but sourced from a sibling HTML render
function (e.g. Console's "Register a physical dataset" leaking onto My
Datasets) must be caught even when copy-fidelity checks pass."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.pipeline.normalize import _extract_view_button_map
from uiforgemax.pipeline.visual_validate import find_cross_view_button_leaks

_HTML = """
function renderMyData(){
  return `<div class="md-row"><button class="btn-xs">Bind</button></div>`;
}
function renderProducerConsole(){
  return `<div class="sh-actions">
    <button class="btn-dk" onclick="openRegister()">Register a physical dataset</button>
    <button class="btn-lt" onclick="openRegistry()">Bind columns</button>
  </div>`;
}
"""


def test_extract_view_button_map_scopes_buttons_per_function():
    import html as html_mod

    view_map = _extract_view_button_map(_HTML, unescape=html_mod.unescape)
    assert view_map["renderMyData"] == ["Bind"]
    assert view_map["renderProducerConsole"] == [
        "Register a physical dataset",
        "Bind columns",
    ]


def _write_run(tmp_path: Path, page_tsx_content: str) -> Path:
    run_dir = tmp_path / "run"
    (run_dir / "plans").mkdir(parents=True)
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)

    import html as html_mod

    view_map = _extract_view_button_map(_HTML, unescape=html_mod.unescape)
    (run_dir / "visual-spec.json").write_text(
        json.dumps({"viewButtonMap": view_map}), encoding="utf-8"
    )
    (run_dir / "plans" / "subtasks.json").write_text(
        json.dumps(
            {
                "subtasks": [
                    {
                        "id": "ST-2",
                        "visualRegion": {"regionId": "renderMyData"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps(
            {
                "create": [
                    {"path": "src/MyDatasetsPage.tsx", "subtaskId": "ST-2", "root": "default"}
                ],
                "modify": [],
            }
        ),
        encoding="utf-8",
    )
    (project / "src" / "MyDatasetsPage.tsx").write_text(page_tsx_content, encoding="utf-8")
    return run_dir, project


def test_flags_button_borrowed_from_sibling_view(tmp_path):
    run_dir, project = _write_run(
        tmp_path,
        '<button className="btn-dk">Register a physical dataset</button>\n'
        '<button className="btn-xs">Bind</button>',
    )
    violations = find_cross_view_button_leaks(run_dir, {"default": project})
    assert len(violations) == 1
    v = violations[0]
    assert v["subtaskId"] == "ST-2"
    assert v["buttonLabel"] == "Register a physical dataset"
    assert v["expectedView"] == "renderMyData"
    assert v["actualOwningViews"] == ["renderProducerConsole"]


def test_no_violation_when_button_belongs_to_its_own_view(tmp_path):
    run_dir, project = _write_run(tmp_path, '<button className="btn-xs">Bind</button>')
    assert find_cross_view_button_leaks(run_dir, {"default": project}) == []


def test_skips_subtask_with_non_literal_region_id(tmp_path):
    run_dir, project = _write_run(
        tmp_path,
        '<button className="btn-dk">Register a physical dataset</button>',
    )
    subtasks_path = run_dir / "plans" / "subtasks.json"
    data = json.loads(subtasks_path.read_text(encoding="utf-8"))
    data["subtasks"][0]["visualRegion"]["regionId"] = "md-list"  # invented id, not a function name
    subtasks_path.write_text(json.dumps(data), encoding="utf-8")

    assert find_cross_view_button_leaks(run_dir, {"default": project}) == []
