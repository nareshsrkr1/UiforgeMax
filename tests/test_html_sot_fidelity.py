"""HTML SoT: html mode on Jira promote, richer extract, exact-copy compliance."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from uiforgemax.config import Config
from uiforgemax.pipeline.classify import build_classification_signals, default_classification
from uiforgemax.pipeline.normalize import (
    _extract_html_structure,
    apply_html_sot_to_compliance,
    harvest_exact_texts_from_components,
    normalize_run,
)
from uiforgemax.pipeline.visual_validate import find_missing_exact_texts
from uiforgemax.state import RunState
from uiforgemax.tools import ToolContext, inputs, lifecycle


_JS_HTML = """
<!doctype html><html><body><script>
function renderMyData(){
  return `<div>
    <h1><span>My datasets</span></h1>
    <button class="btn"><span>Bind</span></button>
  </div>`;
}
const stats = [
  { title: "Datasets Managed", label: "Live" },
];
kpi("Pending governance", 3);
kpi("metric_code", 1);
</script></body></html>
"""


def test_extract_nested_and_js_ui_strings(tmp_path: Path):
    page = tmp_path / "page.html"
    page.write_text(_JS_HTML, encoding="utf-8")
    summary = _extract_html_structure(str(page), run_dir=tmp_path)
    labels = {
        (c.get("text") or c.get("label") or "").lower()
        for c in summary.get("components") or []
    }
    assert "my datasets" in labels
    assert "bind" in labels
    assert "datasets managed" in labels
    assert "pending governance" in labels
    # code-ish first args should not be harvested as UI copy
    assert "metric_code" not in labels
    hints = [h.lower() for h in summary.get("exactTextHints") or []]
    assert "my datasets" in hints
    assert "bind" in hints


def test_apply_html_sot_sets_match_exactly_when_rich():
    components = [
        {"type": "Heading1", "text": "My datasets"},
        {"type": "Button", "label": "Bind"},
        {"type": "Text", "text": "Pending governance"},
        {"type": "Text", "text": "Live"},
    ]
    visual = {
        "htmlDerived": True,
        "components": components,
        "exactTextHints": harvest_exact_texts_from_components(components),
    }
    req: dict = {"compliance": {"matchExactly": False, "exactTextRequirements": []}, "assumptions": []}
    apply_html_sot_to_compliance(req, visual, sot={"primaryHtml": "inputs/page.html"})
    assert req["compliance"]["matchExactly"] is True
    assert req["compliance"]["htmlExactCopy"] is True
    exact = [t.lower() for t in req["compliance"]["exactTextRequirements"]]
    assert "my datasets" in exact
    assert "bind" in exact


def test_sparse_html_does_not_force_match_exactly():
    visual = {
        "htmlDerived": True,
        "components": [{"type": "Button", "label": "OK"}],
        "exactTextHints": ["OK"],
    }
    req: dict = {"compliance": {"matchExactly": False, "exactTextRequirements": []}}
    apply_html_sot_to_compliance(req, visual, sot={"primaryHtml": "inputs/page.html"})
    assert req["compliance"]["matchExactly"] is False
    assert req["compliance"]["exactTextRequirements"] == ["OK"]


def test_normalize_run_harvests_html_exact_text(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "inputs").mkdir(parents=True)
    (run_dir / "inputs" / "page.html").write_text(_JS_HTML, encoding="utf-8")
    (run_dir / "inputs" / "prompt.txt").write_text("Match the attached HTML", encoding="utf-8")
    (run_dir / "request-classification.json").write_text(
        json.dumps({"surface": "ui_only", "requestType": "enhancement", "greenfieldScaffold": False}),
        encoding="utf-8",
    )
    visual, requirements = normalize_run(run_dir, policy="frontend_first")
    assert visual.get("htmlDerived") or visual.get("exactTextHints")
    exact = [t.lower() for t in requirements["compliance"]["exactTextRequirements"]]
    assert "my datasets" in exact
    assert requirements["compliance"].get("htmlExactCopy") is True


def test_classify_has_html_signal_without_html_mode(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "inputs").mkdir(parents=True)
    (run_dir / "inputs" / "page.html").write_text("<h1>Hi</h1>", encoding="utf-8")
    state = RunState(run_id="r", project_root=str(tmp_path / "proj"))
    state.inputs["modes"] = ["jira"]
    signals = build_classification_signals(state, run_dir)
    assert signals["hasHtml"] is True
    cls = default_classification(signals)
    assert cls["surface"] == "ui_only"
    assert "html_sot" in cls.get("changeSignal", [])


def test_add_jira_registers_html_mode():
    runs = tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    os.environ["UIFORGEMAX_USE_FIXTURES"] = "1"
    fixtures = Path(tempfile.mkdtemp()) / "fixtures"
    (fixtures / "jira").mkdir(parents=True)
    html = fixtures / "jira" / "ref.html"
    html.write_text(_JS_HTML, encoding="utf-8")
    issue = {
        "key": "SCRUM-HTMLMODE",
        "summary": "With HTML SoT",
        "labels": [],
        "description": "Use attached HTML",
        "acceptanceCriteria": [{"id": "AC-1", "text": "Match HTML"}],
        "comments": [],
        "attachments": [
            {"filename": "ref.html", "path": str(html), "mimeType": "text/html"},
        ],
        "customFields": {},
    }
    (fixtures / "jira" / "SCRUM-HTMLMODE.json").write_text(json.dumps(issue), encoding="utf-8")
    os.environ["UIFORGEMAX_FIXTURES_ROOT"] = str(fixtures)
    ctx = ToolContext.from_config(cfg)
    workspace = Path(tempfile.mkdtemp())
    run_id = json.loads(lifecycle.start_run(ctx, project_root=str(workspace)))["runId"]
    out = json.loads(inputs.add_jira(ctx, run_id, "SCRUM-HTMLMODE"))
    assert out.get("stop") is False
    state = ctx.store.load(run_id)
    assert "jira" in state.inputs["modes"]
    assert "html" in state.inputs["modes"]


def test_find_missing_exact_texts(tmp_path: Path):
    run_dir = tmp_path / "run"
    proj = tmp_path / "proj"
    proj.mkdir()
    (run_dir / "plans").mkdir(parents=True)
    (run_dir / "requirements.normalized.json").write_text(
        json.dumps(
            {
                "compliance": {
                    "matchExactly": True,
                    "htmlExactCopy": True,
                    "exactTextRequirements": ["My datasets", "Bind"],
                }
            }
        ),
        encoding="utf-8",
    )
    (proj / "App.tsx").write_text("export const App = () => <h1>My dataset</h1>;\n", encoding="utf-8")
    (run_dir / "plans" / "approved-plan.json").write_text(
        json.dumps({"create": [], "modify": [{"path": "App.tsx", "root": "default"}]}),
        encoding="utf-8",
    )
    missing = find_missing_exact_texts(run_dir, {"default": proj})
    assert "My datasets" in missing
    assert "Bind" in missing
