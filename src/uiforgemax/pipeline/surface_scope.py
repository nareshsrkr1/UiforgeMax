"""Filter requirement-map and plans by classified surface (ui_only / api_only)."""

from __future__ import annotations

from typing import Any

from uiforgemax.pipeline.target_sanitize import sanitize_requirement_map

_API_PATH_HINTS = (
    "/routes/", "/api/", "/controllers/", "/handlers/", "/endpoints/",
    "/services/", "/resolvers/", "/graphql/", "data-access",
    "server.ts", "server.js", "server.py", "main.py", "app.py",
    "controller.java", "Controller.java", "Handler.go",
)
_UI_PATH_HINTS = (
    "/pages/", "/views/", "/components/", "/screens/", "/layouts/", "/templates/",
    ".tsx", ".jsx", ".vue", ".svelte", ".html", ".css", ".scss",
    "app.component.ts", "App.vue", "App.svelte",
    "/styles/", "/assets/", "/public/",
)


def apply_surface_to_requirement_map(req_map: dict[str, Any], surface: str) -> dict[str, Any]:
    """Drop irrelevant create/modify actions based on ui_only vs api_only."""
    if surface not in ("ui_only", "api_only"):
        return sanitize_requirement_map(req_map)

    create = list(req_map.get("create", []))
    modify = list(req_map.get("modify", []))

    if surface == "ui_only":
        create = [f for f in create if not _matches_hints(f.get("path", ""), _API_PATH_HINTS)]
        modify = [f for f in modify if not _matches_hints(f.get("path", ""), _API_PATH_HINTS)]
        req_map["apiGaps"] = []
    elif surface == "api_only":
        create = [f for f in create if not _matches_hints(f.get("path", ""), _UI_PATH_HINTS)]
        modify = [f for f in modify if not _matches_hints(f.get("path", ""), _UI_PATH_HINTS)]

    req_map["create"] = create
    req_map["modify"] = modify
    req_map["executionOrder"] = [f["path"] for f in create + modify]
    req_map["stats"] = {
        **req_map.get("stats", {}),
        "createCount": len(create),
        "modifyCount": len(modify),
    }
    req_map["surfaceFilter"] = surface
    # Always strip package.json / project.json / lockfiles etc.
    return sanitize_requirement_map(req_map)


def apply_surface_to_api_resolution(api: dict[str, Any], surface: str) -> dict[str, Any]:
    if surface == "ui_only":
        return {"resolution": [], "gate1Required": False, "source": "surface_ui_only"}
    return api


def _matches_hints(path: str, hints: tuple[str, ...]) -> bool:
    lower = path.lower()
    return any(h.lower() in lower for h in hints)
