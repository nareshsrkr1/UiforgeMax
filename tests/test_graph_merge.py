"""Real Graphify update + merge path (no homemade Nx indexer)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from uiforgemax.config import Config
from uiforgemax.graphify.cli_runner import ensure_graphify_available, run_update
from uiforgemax.graphify.engine import graphify_merge, graphify_update_multi
from uiforgemax.graphify.stack import detect_stack
from uiforgemax.state import Stage, Status
from uiforgemax.tools import ToolContext, inputs, lifecycle, pipeline

_HAS_GRAPHIFY = ensure_graphify_available().get("ok")


def _ctx() -> ToolContext:
    runs = tempfile.mkdtemp()
    cfg = Config(runs_root=runs)
    os.environ["UIFORGEMAX_SKIP_MEDIATION"] = "1"
    return ToolContext.from_config(cfg)


def _write_fastapi_backend(root: Path) -> None:
    app = root / "app"
    app.mkdir(parents=True)
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "main.py").write_text(
        '''
from fastapi import FastAPI
app = FastAPI()

@app.get("/api/health")
def health():
    return {"ok": True}

@app.get("/api/customers")
def list_customers():
    return []
''',
        encoding="utf-8",
    )


def _write_react_ui(root: Path) -> None:
    (root / "serve.py").write_text(
        "def main():\n    print('ui')\n",
        encoding="utf-8",
    )
    (root / "styles.css").write_text("body { background: #fff; }\n", encoding="utf-8")
    (root / "README.md").write_text("# UI\n", encoding="utf-8")


@pytest.mark.skipif(not _HAS_GRAPHIFY, reason="graphifyy not installed")
def test_detect_stack_and_real_update(tmp_path: Path):
    backend = tmp_path / "backend"
    backend.mkdir()
    _write_fastapi_backend(backend)
    stack = detect_stack(backend)
    assert stack["fastapi"] is True
    assert stack["nx"] is False

    meta = run_update(backend)
    assert Path(meta["graphJson"]).exists()
    assert meta["nodeCount"] >= 1
    assert (backend / "graphify-out" / "graph.json").exists()


@pytest.mark.skipif(not _HAS_GRAPHIFY, reason="graphifyy not installed")
def test_update_multi_and_merge(tmp_path: Path):
    ui = tmp_path / "ui"
    backend = tmp_path / "backend"
    ui.mkdir()
    backend.mkdir()
    _write_react_ui(ui)
    _write_fastapi_backend(backend)

    per_root = graphify_update_multi({"ui": ui, "backend": backend})
    assert "ui" in per_root and "backend" in per_root
    assert (ui / "graphify-out" / "graph.json").exists()
    assert (backend / "graphify-out" / "graph.json").exists()

    run_graph = tmp_path / "run-graph"
    merged = graphify_merge(
        per_root,
        surface="full_stack",
        default_root=tmp_path,
        run_graph_dir=run_graph,
    )
    assert merged["merged"] is True
    assert Path(merged["mergedGraph"]).exists()
    assert (run_graph / "merged.json").exists()


@pytest.mark.skipif(not _HAS_GRAPHIFY, reason="graphifyy not installed")
def test_pipeline_writes_graphify_out_artifacts(tmp_path: Path):
    from uiforgemax.session import load_session, save_session

    ctx = _ctx()
    parent = tmp_path / "Project"
    ui = parent / "ui"
    backend = parent / "backend"
    ui.mkdir(parents=True)
    backend.mkdir(parents=True)
    _write_react_ui(ui)
    _write_fastapi_backend(backend)

    original = load_session()
    try:
        resp = json.loads(
            lifecycle.start_run(
                ctx,
                project_root=str(parent),
                components=json.dumps({"ui": str(ui), "backend": str(backend)}),
            )
        )
        run_id = resp["runId"]
        inputs.add_prompt(ctx, run_id, "Enhance customers list on ui and api")
        pipeline.advance(ctx, run_id)

        assert (ui / "graphify-out" / "graph.json").exists()
        assert (backend / "graphify-out" / "graph.json").exists()

        run_dir = ctx.store.run_dir(run_id)
        assert (run_dir / "graph" / "by-root" / "ui" / "graph.json").exists()
        assert (run_dir / "graph" / "by-root" / "backend" / "graph.json").exists()
        assert (run_dir / "graph" / "queries.json").exists()
        assert (run_dir / "graph" / "query-results.json").exists()
        req_map = json.loads((run_dir / "graph" / "requirement-map.json").read_text(encoding="utf-8"))
        assert req_map.get("source") == "graphify-cli"
        assert "CustomerListPage" not in json.dumps(req_map)

        state = ctx.store.load(run_id)
        assert any(h.stage == Stage.GRAPHIFY_UPDATE.value for h in state.history)
        assert state.status in {
            Status.AWAITING_PLAN_APPROVAL,
            Status.AWAITING_API_APPROVAL,
            Status.AWAITING_MEDIATION,
            Status.GRAPH_MERGED,
            Status.GRAPH_QUERIED,
            Status.REQUIREMENT_MAPPED,
        }
    finally:
        save_session(original)


def test_detect_stack_nx_flag(tmp_path: Path):
    root = tmp_path / "mono"
    root.mkdir()
    (root / "nx.json").write_text("{}", encoding="utf-8")
    stack = detect_stack(root)
    assert stack["nx"] is True
    assert stack["primary"] == "nx"
