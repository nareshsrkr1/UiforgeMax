"""Smoke tests for P0 gap fixes in normalize.py and implement.py."""
import json
import tempfile
from pathlib import Path

# ── Gap 1: visual spec stub removal ──────────────────────────────────────────
from uiforgemax.pipeline.normalize import (
    _VISUAL_SPEC_UNCONFIRMED_SENTINEL,
    _extract_html_structure,
    _extract_prompt_intent,
    build_visual_spec,
    build_from_prompt,
    apply_overrides,
)


def test_visual_spec_image_is_unconfirmed_sentinel():
    """When images are present the spec must be provisional — never demo data."""
    sot = {"primaryImage": "inputs/attachments/wireframe.png", "images": [{"path": "inputs/attachments/wireframe.png"}]}
    spec = build_visual_spec("inputs/attachments/wireframe.png", sot=sot, has_image=True)
    assert spec["visualSpecUnconfirmed"] is True, "must be marked unconfirmed"
    assert spec["pendingMediation"] == _VISUAL_SPEC_UNCONFIRMED_SENTINEL
    assert spec["components"] == [], "components must be empty — only mediation fills these"
    assert spec["layout"]["regions"] == []
    # No hardcoded demo names
    spec_str = json.dumps(spec)
    assert "Export CSV" not in spec_str
    assert "Customers" not in spec_str
    assert "DataGrid" not in spec_str
    print("PASS: visual_spec_image_is_unconfirmed_sentinel")


def test_visual_spec_html_extracts_real_structure():
    """HTML input must produce a real DOM-derived spec, not a demo fixture."""
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", delete=False, encoding="utf-8") as f:
        f.write("""<!DOCTYPE html><html><body>
        <header>My App</header>
        <nav>Home | About</nav>
        <main>
            <h1>Task Manager</h1>
            <input placeholder="Search tasks...">
            <button>Add Task</button>
            <button>Export</button>
            <table><thead><tr><th>Title</th><th>Status</th><th>Due</th></tr></thead></table>
        </main>
        </body></html>""")
        html_path = f.name

    sot = {"primaryHtml": html_path}
    spec = build_visual_spec(html_path, sot=sot, has_image=False)
    assert spec["visualSpecUnconfirmed"] is False
    assert spec.get("htmlDerived") is True

    labels = [c.get("label") or c.get("text") or c.get("placeholder") for c in spec["components"]]
    types = [c["type"] for c in spec["components"]]

    # Must have extracted from the HTML
    assert any("Add Task" in (l or "") for l in labels), f"Add Task button not found; labels={labels}"
    assert any("Export" in (l or "") for l in labels), f"Export button not found; labels={labels}"
    assert any("Search tasks" in (l or "") for l in labels), f"Search input not found; labels={labels}"
    assert "Table" in types, f"Table not found; types={types}"
    assert "Heading1" in types

    # No hardcoded demo names
    spec_str = json.dumps(spec)
    assert "Export CSV" not in spec_str, "old stub string must not appear"
    assert "customer" not in spec_str.lower()
    print(f"PASS: visual_spec_html_extracts — components={[c['type'] for c in spec['components']]}")


def test_visual_spec_prompt_only_extracts_intent():
    """Prompt-only must extract intent keywords, not demo data."""
    spec = build_visual_spec("prompt", prompt="Build a task management table with search and delete button", has_image=False)
    assert spec["visualSpecUnconfirmed"] is False
    assert spec.get("promptDerived") is True
    types = [c["type"] for c in spec["components"]]
    assert "Table/DataGrid" in types
    assert "Search/Filter" in types
    spec_str = json.dumps(spec)
    assert "Export CSV" not in spec_str
    assert "customer" not in spec_str.lower()
    print(f"PASS: visual_spec_prompt_intent — components={types}")


# ── Gap 2: prompt targetApp hardcoding removed ───────────────────────────────

def test_prompt_no_classification_uses_generic():
    """Without classification, targetApp must be generic 'app', never 'customer-portal'."""
    req = build_from_prompt("Build a task tracker", "frontend_first")
    assert req["policy"]["targetApp"] == "app", f"Expected 'app', got '{req['policy']['targetApp']}'"
    assert req["policy"]["targetDomain"] == "product"
    assert "customer-portal" not in json.dumps(req)
    assert "Customer" not in json.dumps(req.get("dataNeeds", []))
    print("PASS: prompt_no_classification_uses_generic")


def test_prompt_with_classification_uses_targets():
    """When classification has targets, they must be respected exactly."""
    cls = {"targets": {"apps": ["inventory-portal"], "domains": ["inventory"]}}
    req = build_from_prompt("Build an inventory management UI", "frontend_first", classification=cls)
    assert req["policy"]["targetApp"] == "inventory-portal"
    assert req["policy"]["targetDomain"] == "inventory"
    print("PASS: prompt_with_classification_uses_targets")


def test_prompt_with_empty_classification_targets_uses_generic():
    """Empty targets list in classification falls back to generic."""
    cls = {"targets": {"apps": [], "domains": []}}
    req = build_from_prompt("Build something", "frontend_first", classification=cls)
    assert req["policy"]["targetApp"] == "app"
    assert req["policy"]["targetDomain"] == "product"
    print("PASS: prompt_empty_targets_falls_back")


# ── Gap 2b: apply_overrides is comprehensive ─────────────────────────────────

def test_overrides_color_theme():
    req = {
        "overrides": [
            {"isOverride": True, "author": "alice", "created": "2024-01-01", "body": "use blue primary theme"}
        ],
        "assumptions": [],
        "scope": {"in": [], "out": []},
    }
    apply_overrides(req)
    assert any("blue" in a for a in req["assumptions"]), f"assumptions={req['assumptions']}"
    print("PASS: overrides_color_theme")


def test_overrides_scope_out():
    req = {
        "overrides": [
            {"isOverride": True, "author": "bob", "created": "2024-01-02", "body": "don't create a sidebar"}
        ],
        "assumptions": [],
        "scope": {"in": [], "out": []},
    }
    apply_overrides(req)
    assert "a sidebar" in req["scope"]["out"] or any("sidebar" in x for x in req["scope"]["out"]), f"scope_out={req['scope']['out']}"
    print("PASS: overrides_scope_out")


def test_overrides_change():
    req = {
        "overrides": [
            {"isOverride": True, "author": "carol", "created": "2024-01-03", "body": "change the export button to upload"}
        ],
        "assumptions": [],
        "scope": {"in": [], "out": []},
    }
    apply_overrides(req)
    assert any("upload" in a.lower() for a in req["assumptions"]), f"assumptions={req['assumptions']}"
    print("PASS: overrides_change")


# ── Gap 3: PartialImplementError ─────────────────────────────────────────────

def test_partial_implement_raises():
    """apply_plan must raise PartialImplementError when some files skipped."""
    from uiforgemax.pipeline.implement import apply_plan, PartialImplementError
    import os

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Create one real file (for the "create" path)
        plan = {
            "create": [
                {"path": "new_file.txt", "purpose": "new", "content": "hello"},
            ],
            "modify": [
                # This file exists but has no content= and no templateId/patchId
                # → _generic_modify raises ValueError → skipped
                {"path": "existing.py", "purpose": "patch it"},
            ],
            "executionOrder": ["new_file.txt", "existing.py"],
        }
        (root / "existing.py").write_text("# existing", encoding="utf-8")

        try:
            apply_plan(root, plan)
            assert False, "Should have raised PartialImplementError"
        except PartialImplementError as exc:
            assert len(exc.changed) == 1
            assert "new_file.txt" in exc.changed
            assert len(exc.skipped) == 1
            assert exc.skipped[0]["path"] == "existing.py"
            assert "PARTIAL IMPLEMENT" in str(exc)
            print(f"PASS: partial_implement_raises — changed={exc.changed}, skipped=[{exc.skipped[0]['path']}]")


if __name__ == "__main__":
    test_visual_spec_image_is_unconfirmed_sentinel()
    test_visual_spec_html_extracts_real_structure()
    test_visual_spec_prompt_only_extracts_intent()
    test_prompt_no_classification_uses_generic()
    test_prompt_with_classification_uses_targets()
    test_prompt_with_empty_classification_targets_uses_generic()
    test_overrides_color_theme()
    test_overrides_scope_out()
    test_overrides_change()
    test_partial_implement_raises()
    print("\n✅  ALL P0 SMOKE TESTS PASSED")
