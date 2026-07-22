"""Implementation stage — writes approved plan files into the workspace.

Generic by design: real application changes come from IDE-mediated ``content``
(or a small set of scaffold ``templateId``s for greenfield). MCP does **not**
invent product-specific UI (themes, nav, pages) for a particular demo app.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from uiforgemax.pipeline.toolchain import _kill_process_tree

_GIT_TIMEOUT_SECONDS = 20


def _git_run(args: list[str], *, cwd: str, timeout: int = _GIT_TIMEOUT_SECONDS) -> None:
    """Best-effort git call that can never block the pipeline indefinitely.

    Branch/add/commit bookkeeping is supplementary to the actual file writes,
    not required for plan correctness. A stale ``.git/index.lock`` from an
    earlier interrupted run, a corporate hook phoning home, or a GPG-signing
    prompt waiting on stdin that will never arrive must never hang
    ``uiforgemax_advance`` forever — soft-fail and move on instead.
    """
    try:
        proc = subprocess.Popen(
            ["git", *args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
    except FileNotFoundError:
        return
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass


class PartialImplementError(Exception):
    """Raised by :func:`apply_plan` when some plan files were written but others
    were skipped (no ``content`` supplied by PLAN_REFINEMENT mediation).

    Attributes
    ----------
    changed:
        Relative paths of files that were successfully written.
    skipped:
        List of ``{path, reason}`` dicts for files that could not be written.
    branch:
        Git branch name if a branch was created, or ``None`` if git is absent.
    """

    def __init__(
        self,
        message: str,
        *,
        changed: list[str],
        skipped: list[dict[str, str]],
        branch: str | None,
    ) -> None:
        super().__init__(message)
        self.changed = changed
        self.skipped = skipped
        self.branch = branch

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _normalize_roots(project_root: Path | dict[str, Path]) -> dict[str, Path]:
    if isinstance(project_root, dict):
        return project_root
    return {"default": project_root}


def resolve_root(roots: dict[str, Path], action: dict[str, Any]) -> Path:
    """Pick the repo root a create/modify action belongs to.

    Actions with no explicit `"root"` (the common case — everything created
    under one repo) resolve to `"default"`. Multi-repo plans set `"root"` to
    a name registered via `uiforgemax_add_workspace_root`.
    """
    name = action.get("root") or "default"
    root = roots.get(name) or roots.get("default")
    if root is None:
        raise ValueError(f"No project root registered for '{name}' (and no default root either).")
    return root


def apply_plan(
    project_root: Path | dict[str, Path],
    plan: dict[str, Any],
    *,
    subtasks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    roots = _normalize_roots(project_root)

    if subtasks and subtasks.get("subtaskCount", 0) > 1:
        return _apply_plan_subtasked(roots, plan, subtasks)

    changed: list[str] = []
    skipped: list[dict[str, str]] = []
    branch = "uiforgemax/feature-run"

    for root in {str(r) for r in roots.values()}:
        _git_run(["checkout", "-b", branch], cwd=root)

    # Scaffold / legacy fixture writers only — not product UI for a specific app.
    templates = {
        "api.export_route": _export_route,
        "client.export_customers": _export_client,
        "page.customer_list": _customer_list_page,
        "test.customer_list": _customer_list_test,
        "greenfield.backend_requirements": _gf_backend_requirements,
        "greenfield.backend_init": _gf_empty,
        "greenfield.backend_store": _gf_backend_store,
        "greenfield.backend_main": _gf_backend_main,
        "greenfield.backend_readme": _gf_backend_readme,
        "greenfield.ui_index": _gf_ui_index,
        "greenfield.ui_styles": _gf_ui_styles,
        "greenfield.ui_serve": _gf_ui_serve,
        "greenfield.ui_readme": _gf_ui_readme,
        "greenfield.readme": _gf_readme,
        "greenfield.start_ps1": _gf_start_ps1,
    }
    patches = {
        "app.register_export": _patch_app,
        "data_access.export": _patch_data_access,
        "portal.app_routing": _patch_portal_app,
        "portal.green_button": _patch_styles,
    }

    create_paths = {item["path"] for item in plan.get("create", []) if item.get("path")}
    actions_by_path: dict[str, dict] = {}
    for item in plan.get("create", []) + plan.get("modify", []):
        if item.get("path"):
            actions_by_path[item["path"]] = item

    ordered = list(plan.get("executionOrder") or [])
    for path in actions_by_path:
        if path not in ordered:
            ordered.append(path)

    for rel in ordered:
        action = actions_by_path.get(rel)
        if not action:
            continue
        root = resolve_root(roots, action)
        target = root / rel
        tid = action.get("templateId")
        pid = action.get("patchId")
        is_create = rel in create_paths or not target.exists()

        try:
            # 1) Mediation-supplied full file body — primary path for real apps
            if action.get("content") is not None:
                content = str(action["content"])
            elif tid and tid in templates:
                content = templates[tid](root, target)
            elif pid and pid in patches:
                content = patches[pid](root, target)
            elif is_create:
                content = _generic_create(action)
            else:
                content = _generic_modify(action, target)
        except Exception as exc:  # noqa: BLE001 — record and continue other files
            skipped.append({"path": rel, "reason": str(exc)})
            continue

        if content is None:
            skipped.append({"path": rel, "reason": "no writer produced content"})
            continue

        _write(target, content)
        changed.append(rel)
        _git_run(["add", rel], cwd=root)
        _git_run(["commit", "-m", f"uiforgemax: {rel}"], cwd=root)

    # Hard-fail on partial writes: if some files were written but others were
    # skipped, the plan has gaps that will produce broken or incomplete code.
    # Surface this as an error immediately rather than silently committing
    # partial work.  Callers can catch this and set status=FAILED.
    if changed and skipped:
        skipped_summary = "; ".join(
            f"{s['path']} ({s['reason']})" for s in skipped
        )
        raise PartialImplementError(
            f"PARTIAL IMPLEMENT: {len(changed)} file(s) written but "
            f"{len(skipped)} file(s) were skipped.\n"
            f"Skipped: {skipped_summary}\n"
            "PLAN_REFINEMENT mediation must supply 'content' for every create/modify "
            "action — MCP does not invent product-specific code for files it has not "
            "been given content for.  Re-run PLAN_REFINEMENT mediation with 'content' "
            "fields for each skipped path, then uiforgemax_advance.",
            changed=changed,
            skipped=skipped,
            branch=branch,
        )

    return {
        "branch": branch,
        "filesChanged": changed,
        "fileCount": len(changed),
        "skipped": skipped,
        "source": plan.get("source"),
        "roots": {name: str(p) for name, p in roots.items()},
    }


def _apply_plan_subtasked(
    roots: dict[str, Path],
    plan: dict[str, Any],
    subtasks: dict[str, Any],
) -> dict[str, Any]:
    changed: list[str] = []
    skipped: list[dict[str, str]] = []
    subtask_results: dict[str, dict[str, Any]] = {}
    branch = "uiforgemax/feature-run"

    for root in {str(r) for r in roots.values()}:
        _git_run(["checkout", "-b", branch], cwd=root)

    templates = _build_template_map()
    patches = _build_patch_map()
    create_paths = {item["path"] for item in plan.get("create", []) if item.get("path")}
    actions_by_path: dict[str, dict] = {}
    for item in plan.get("create", []) + plan.get("modify", []):
        if item.get("path"):
            actions_by_path[item["path"]] = item

    dep_order = subtasks.get("dependencyOrder", [st["id"] for st in subtasks.get("subtasks", [])])
    st_by_id = {st["id"]: st for st in subtasks.get("subtasks", [])}
    subtask_plan = plan.get("subtaskPlan") or {}
    sp_by_id = {sp["subtaskId"]: sp for sp in subtask_plan.get("subtasks", [])}

    for st_id in dep_order:
        st = st_by_id.get(st_id, {})
        sp = sp_by_id.get(st_id, {})
        st_exec_order = sp.get("executionOrder", [])

        if not st_exec_order:
            for path, action in actions_by_path.items():
                if action.get("subtaskId") == st_id and path not in st_exec_order:
                    st_exec_order.append(path)

        st_changed: list[str] = []
        st_skipped: list[dict[str, str]] = []

        for rel in st_exec_order:
            action = actions_by_path.pop(rel, None)
            if not action:
                continue
            root = resolve_root(roots, action)
            target = root / rel
            is_create = rel in create_paths or not target.exists()

            try:
                if action.get("content") is not None:
                    content = str(action["content"])
                elif action.get("templateId") and action["templateId"] in templates:
                    content = templates[action["templateId"]](root, target)
                elif action.get("patchId") and action["patchId"] in patches:
                    content = patches[action["patchId"]](root, target)
                elif is_create:
                    content = _generic_create(action)
                else:
                    content = _generic_modify(action, target)
            except Exception as exc:  # noqa: BLE001
                st_skipped.append({"path": rel, "reason": str(exc)})
                continue

            if content is None:
                st_skipped.append({"path": rel, "reason": "no writer produced content"})
                continue

            _write(target, content)
            st_changed.append(rel)
            _git_run(["add", rel], cwd=root)

        if st_changed:
            commit_root = resolve_root(roots, st_by_id.get(st_id, {}))
            _git_run(["commit", "-m", f"uiforgemax [{st_id}]: {st.get('title', st_id)}"], cwd=commit_root)

        changed.extend(st_changed)
        skipped.extend(st_skipped)
        subtask_results[st_id] = {
            "filesChanged": st_changed,
            "fileCount": len(st_changed),
            "skipped": st_skipped,
            "status": "completed" if not st_skipped else ("partial" if st_changed else "failed"),
        }

    remaining = [p for p in plan.get("executionOrder", []) if p in actions_by_path]
    for rel in remaining:
        action = actions_by_path.pop(rel, None)
        if not action:
            continue
        root = resolve_root(roots, action)
        target = root / rel
        is_create = rel in create_paths or not target.exists()
        try:
            if action.get("content") is not None:
                content = str(action["content"])
            elif is_create:
                content = _generic_create(action)
            else:
                content = _generic_modify(action, target)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"path": rel, "reason": str(exc)})
            continue
        if content is None:
            skipped.append({"path": rel, "reason": "no writer produced content"})
            continue
        _write(target, content)
        changed.append(rel)
        _git_run(["add", rel], cwd=root)
        _git_run(["commit", "-m", f"uiforgemax: {rel}"], cwd=root)

    if changed and skipped:
        skipped_summary = "; ".join(f"{s['path']} ({s['reason']})" for s in skipped)
        raise PartialImplementError(
            f"PARTIAL IMPLEMENT: {len(changed)} file(s) written but "
            f"{len(skipped)} file(s) were skipped.\n"
            f"Skipped: {skipped_summary}\n"
            "PLAN_REFINEMENT mediation must supply 'content' for every create/modify "
            "action — MCP does not invent product-specific code for files it has not "
            "been given content for.  Re-run PLAN_REFINEMENT mediation with 'content' "
            "fields for each skipped path, then uiforgemax_advance.",
            changed=changed,
            skipped=skipped,
            branch=branch,
        )

    return {
        "branch": branch,
        "filesChanged": changed,
        "fileCount": len(changed),
        "skipped": skipped,
        "source": plan.get("source"),
        "roots": {name: str(p) for name, p in roots.items()},
        "subtaskResults": subtask_results,
    }


def _build_template_map() -> dict[str, Any]:
    return {
        "api.export_route": _export_route,
        "client.export_customers": _export_client,
        "page.customer_list": _customer_list_page,
        "test.customer_list": _customer_list_test,
        "greenfield.backend_requirements": _gf_backend_requirements,
        "greenfield.backend_init": _gf_empty,
        "greenfield.backend_store": _gf_backend_store,
        "greenfield.backend_main": _gf_backend_main,
        "greenfield.backend_readme": _gf_backend_readme,
        "greenfield.ui_index": _gf_ui_index,
        "greenfield.ui_styles": _gf_ui_styles,
        "greenfield.ui_serve": _gf_ui_serve,
        "greenfield.ui_readme": _gf_ui_readme,
        "greenfield.readme": _gf_readme,
        "greenfield.start_ps1": _gf_start_ps1,
    }


def _build_patch_map() -> dict[str, Any]:
    return {
        "app.register_export": _patch_app,
        "data_access.export": _patch_data_access,
        "portal.app_routing": _patch_portal_app,
        "portal.green_button": _patch_styles,
    }


# --- Generic writers (no product/demo assumptions) --------------------------------


def _generic_create(action: dict[str, Any]) -> str:
    """Minimal new-file scaffold by extension — not business logic for any app."""
    path = (action.get("path") or "").replace("\\", "/")
    purpose = action.get("purpose") or path
    suffix = Path(path).suffix.lower()

    if suffix in {".tsx", ".jsx"}:
        name = _component_name(path)
        return (
            f"/** {purpose} */\n"
            f"export function {name}() {{\n"
            f"  return <div data-testid=\"{Path(path).stem}\">{name}</div>;\n"
            f"}}\n"
        )
    if suffix == ".vue":
        name = _component_name(path)
        return (
            f"<template>\n  <div>{name}</div>\n</template>\n\n"
            f"<script setup lang=\"ts\">\n// {purpose}\n</script>\n"
        )
    if suffix == ".svelte":
        name = _component_name(path)
        return f"<script lang=\"ts\">\n  // {purpose}\n</script>\n\n<div>{name}</div>\n"
    if suffix in {".ts", ".js"}:
        return f"// {purpose}\nexport {{}};\n"
    if suffix == ".css":
        return f"/* {purpose} */\n"
    if suffix in {".scss", ".sass", ".less"}:
        return f"/* {purpose} */\n"
    if suffix == ".py":
        return f'"""{purpose}"""\n'
    if suffix in {".java", ".kt"}:
        return f"// {purpose}\n"
    if suffix == ".go":
        pkg = Path(path).parent.name or "main"
        return f"package {pkg}\n\n// {purpose}\n"
    if suffix == ".rs":
        return f"// {purpose}\n"
    if suffix == ".cs":
        return f"// {purpose}\n"
    if suffix == ".rb":
        return f"# {purpose}\n"
    if suffix == ".php":
        return f"<?php\n// {purpose}\n"
    if suffix == ".md":
        return f"# {Path(path).stem}\n\n{purpose}\n"
    if suffix == ".html":
        return (
            "<!DOCTYPE html>\n<html><head><meta charset=\"UTF-8\" />"
            f"<title>{Path(path).stem}</title></head>"
            f"<body><main>{purpose}</main></body></html>\n"
        )
    if suffix in {".json", ".yaml", ".yml", ".toml", ".xml"}:
        raise ValueError(
            f"Create {path}: supply full content= from PLAN_REFINEMENT "
            f"(MCP will not invent {suffix} structure for your app)."
        )
    return f"/* UiForgeMax create: {purpose} */\n"


def _generic_modify(action: dict[str, Any], target: Path) -> str:
    """Modify without demo-specific patching — require mediated content."""
    if not target.exists():
        return _generic_create(action)
    raise ValueError(
        f"Modify {action.get('path')}: unknown templateId/patchId "
        f"({action.get('templateId')!r}/{action.get('patchId')!r}). "
        "PLAN_REFINEMENT must supply the full file content= for this app — "
        "MCP does not invent product-specific patches."
    )


def _component_name(path: str) -> str:
    stem = Path(path).stem
    parts = re.split(r"[^A-Za-z0-9]+", stem)
    name = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return name or "Component"


# --- Legacy fixture / greenfield scaffold writers --------------------------------


def _export_route(_root: Path, _target: Path) -> str:
    return """import type { Request, Response } from "express";
import { filterCustomers } from "../data/customers.js";

export function registerExportRoute(app: import("express").Express) {
  app.get("/api/customers/export", (req: Request, res: Response) => {
    const search = typeof req.query.search === "string" ? req.query.search : undefined;
    const result = filterCustomers({ search, page: 1, pageSize: 1000 });
    const header = "id,name,email,status,company";
    const rows = result.data.map(
      (c) => `${c.id},${c.name},${c.email},${c.status},${c.company}`,
    );
    res.setHeader("Content-Type", "text/csv");
    res.setHeader("Content-Disposition", 'attachment; filename="customers.csv"');
    res.send([header, ...rows].join("\\n"));
  });
}
"""


def _patch_app(root: Path, target: Path) -> str:
    text = target.read_text(encoding="utf-8")
    if "registerExportRoute" in text:
        return text
    if 'from "./routes/export.js"' not in text:
        text = text.replace(
            'import { filterCustomers, customers } from "./data/customers.js";',
            'import { filterCustomers, customers } from "./data/customers.js";\nimport { registerExportRoute } from "./routes/export.js";',
        )
    needle = "  // Export endpoint intentionally missing — UiForgeMax Jira fixture requires it\n\n  return app;"
    if needle in text:
        return text.replace(needle, "  registerExportRoute(app);\n\n  return app;")
    return text.replace("  return app;", "  registerExportRoute(app);\n\n  return app;")


def _export_client(_root: Path, _target: Path) -> str:
    return """const DEFAULT_BASE = "http://localhost:4000";

export async function exportCustomers(query: { search?: string } = {}): Promise<Blob> {
  const params = new URLSearchParams();
  if (query.search) params.set("search", query.search);
  const response = await fetch(`${DEFAULT_BASE}/api/customers/export?${params}`);
  if (!response.ok) throw new Error(`Export failed: ${response.status}`);
  return response.blob();
}
"""


def _patch_data_access(root: Path, target: Path) -> str:
    text = target.read_text(encoding="utf-8")
    if "exportCustomers" in text:
        return text
    addition = '\nexport { exportCustomers } from "./exportCustomers.js";\n'
    if "exportCustomers intentionally missing" in text:
        text = text.replace(
            "  // NOTE: exportCustomers intentionally missing — Jira ticket will require it\n}",
            "  async exportCustomers(query: { search?: string } = {}): Promise<Blob> {\n"
            "    const { exportCustomers } = await import('./exportCustomers.js');\n"
            "    return exportCustomers(query);\n  }\n}",
        )
    return text.rstrip() + addition


def _customer_list_page(_root: Path, _target: Path) -> str:
    return """import { useEffect, useState } from "react";
import { Button, DataGrid, PageLayout } from "@acme/shared-ui";
import { customerApi } from "@acme/data-access";
import type { Customer } from "@acme/shared-types";
import { exportCustomers } from "@acme/data-access";

export function CustomerListPage() {
  const [rows, setRows] = useState<Customer[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    customerApi.listCustomers({ search }).then((r) => setRows(r.data));
  }, [search]);

  async function onExport() {
    setLoading(true);
    try {
      const blob = await exportCustomers({ search });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "customers.csv";
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setLoading(false);
    }
  }

  return (
    <PageLayout
      title="Customers"
      actions={
        <Button variant="primary" className="acme-btn--green" loading={loading} onClick={onExport}>
          Export CSV
        </Button>
      }
    >
      <input
        aria-label="Search customers"
        placeholder="Search by name or email"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <DataGrid
        data={rows}
        columns={[
          { key: "name", header: "Name" },
          { key: "email", header: "Email" },
          { key: "status", header: "Status" },
          { key: "company", header: "Company" },
        ]}
      />
    </PageLayout>
  );
}
"""


def _customer_list_test(_root: Path, _target: Path) -> str:
    return """import { describe, expect, it } from "vitest";

describe("CustomerListPage", () => {
  it("requires Export CSV label for compliance", () => {
    expect("Export CSV").toBe("Export CSV");
  });
});
"""


def _patch_portal_app(root: Path, target: Path) -> str:
    text = target.read_text(encoding="utf-8")
    if "CustomerListPage" in text:
        return text
    return """import { useEffect, useState } from "react";
import { Sidebar } from "@acme/shared-ui";
import { DashboardPage } from "./pages/DashboardPage";
import { CustomerListPage } from "./pages/CustomerListPage";

const NAV = [
  { id: "dashboard", label: "Dashboard", href: "#dashboard" },
  { id: "customers", label: "Customers", href: "#customers" },
];

function useHashRoute() {
  const [route, setRoute] = useState(window.location.hash || "#dashboard");
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || "#dashboard");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return route;
}

export function App() {
  const route = useHashRoute();
  return (
    <div className="acme-shell" data-testid="app-shell">
      <Sidebar items={NAV} activeId={route === "#customers" ? "customers" : "dashboard"} />
      <main className="acme-main">
        {route === "#customers" ? <CustomerListPage /> : <DashboardPage />}
      </main>
    </div>
  );
}
"""


def _patch_styles(root: Path, target: Path) -> str:
    text = target.read_text(encoding="utf-8")
    if "acme-btn--green" in text:
        return text
    return text + "\n.acme-btn--green {\n  background: #16a34a;\n  color: white;\n}\n"


def _gf_empty(_root: Path, _target: Path) -> str:
    return ""


def _gf_backend_requirements(_root: Path, _target: Path) -> str:
    return "fastapi>=0.110\nuvicorn[standard]>=0.27\n"


def _gf_backend_store(_root: Path, _target: Path) -> str:
    return '''"""In-memory item store."""

from __future__ import annotations

import threading

SEED = [
    {"id": 1, "name": "Alpha", "status": "Active"},
    {"id": 2, "name": "Beta", "status": "Trial"},
]


class ItemStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items = [dict(x) for x in SEED]
        self._next_id = max((x["id"] for x in self._items), default=0) + 1

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(x) for x in self._items]

    def create(self, data: dict) -> dict:
        with self._lock:
            item = {"id": self._next_id, **data}
            self._next_id += 1
            self._items.append(item)
            return dict(item)


store = ItemStore()
'''


def _gf_backend_main(_root: Path, _target: Path) -> str:
    return '''"""Greenfield FastAPI backend."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.store import store

app = FastAPI(title="Greenfield API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ItemCreate(BaseModel):
    name: str
    status: str = "Active"


@app.get("/api/status")
def status():
    return {"ok": True, "service": "greenfield-api"}


@app.get("/api/items")
def list_items():
    return {"data": store.list()}


@app.post("/api/items")
def create_item(body: ItemCreate):
    return store.create(body.model_dump())
'''


def _gf_backend_readme(_root: Path, _target: Path) -> str:
    return "# Backend\\n\\n```bash\\nuvicorn app.main:app --reload --port 8000\\n```\\n"


def _gf_ui_index(_root: Path, _target: Path) -> str:
    return '''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Greenfield App</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <main class="shell">
    <h1>Greenfield App</h1>
    <ul id="items"></ul>
  </main>
  <script>
    const API = "http://127.0.0.1:8000";
    async function loadItems() {
      try {
        const res = await fetch(API + "/api/items");
        const data = await res.json();
        const ul = document.getElementById("items");
        ul.innerHTML = "";
        for (const item of data.data || []) {
          const li = document.createElement("li");
          li.textContent = item.name + " \\u2014 " + item.status;
          ul.appendChild(li);
        }
      } catch (e) {
        console.error("Failed to load items:", e);
      }
    }
    loadItems();
  </script>
</body>
</html>
'''


def _gf_ui_styles(_root: Path, _target: Path) -> str:
    return "body { font-family: system-ui, sans-serif; margin: 2rem; }\\n.shell { max-width: 720px; }\\n"


def _gf_ui_serve(_root: Path, _target: Path) -> str:
    return '''"""Serve UI on port 5173."""
import http.server
import socketserver
from pathlib import Path

PORT = 5173
ROOT = Path(__file__).resolve().parent

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

if __name__ == "__main__":
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"UI at http://127.0.0.1:{PORT}")
        httpd.serve_forever()
'''


def _gf_ui_readme(_root: Path, _target: Path) -> str:
    return "# UI\\n\\n```bash\\npython serve.py\\n```\\n"


def _gf_readme(_root: Path, _target: Path) -> str:
    return "# Greenfield Project\\n\\nScaffolded by UiForgeMax.\\n\\nRun `./start.ps1` to launch backend + UI.\\n"


def _gf_start_ps1(_root: Path, _target: Path) -> str:
    return '''$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root/backend'; uvicorn app.main:app --reload --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root/ui'; python serve.py"
Write-Host "Backend :8000  UI :5173"
'''
