"""Resolve Node/npm (and peers) and ensure project dependencies are installed.

MCP shells on Windows often lack PATH entries that interactive terminals have.
This module finds real binaries and runs allowlisted project installs so
TEST_GENERATION commands (vitest, pytest wrappers, etc.) can actually run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from uiforgemax.env_flags import install_timeout_seconds


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its descendants.

    ``Popen.kill()`` on Windows only signals the immediate child. With
    ``shell=True`` that child is ``cmd.exe``, which has already spawned a
    grandchild (e.g. ``npm.cmd`` -> ``node.exe``). The grandchild survives,
    keeps stdout/stderr pipe handles open, and ``communicate()`` then hangs
    forever waiting for EOF — even though ``TimeoutExpired`` already fired.
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def run_with_timeout(
    cmd: str,
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> SimpleNamespace:
    """``subprocess.run(shell=True, timeout=...)`` that cannot hang past ``timeout``.

    Returns an object with ``returncode``, ``stdout``, ``stderr``, ``timed_out``.
    On timeout the whole process tree is killed (see ``_kill_process_tree``) so
    the caller reliably gets control back instead of blocking indefinitely.
    """
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        shell=True,
        text=True,
        stdin=subprocess.DEVNULL,  # never inherit the MCP server's JSON-RPC stdin
        # pipe — npm/node reading stdin for any reason (a prompt, a postinstall
        # script waiting on input) must get instant EOF, not hang forever on a
        # pipe that will never deliver input. Same fix as the graphify hang.
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        **popen_kwargs,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return SimpleNamespace(returncode=proc.returncode, stdout=stdout, stderr=stderr, timed_out=False)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return SimpleNamespace(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=True,
        )


def which_tool(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def subprocess_env() -> dict[str, str]:
    """Env for test/install subprocesses.

    Prefers tools already on PATH. If discovery found binaries under Program Files
    (etc.), prepend those dirs so thin MCP PATHs still work — without requiring
    UIFORGEMAX_NODE / UIFORGEMAX_NPM in mcp.json.

    Strips PYTHONPATH / UIFORGEMAX_* the same way graphify subprocesses do — the
    MCP server is launched with PYTHONPATH pointing at UiForgeMax's own src/, and
    npm/node/any subprocess-spawned Python tooling has no business inheriting it.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for key in list(env):
        if key.startswith("UIFORGEMAX_"):
            env.pop(key, None)
    bins: list[str] = []
    node = resolve_node()
    npm = resolve_npm()
    if node:
        bins.append(str(Path(node).parent))
    if npm:
        bins.append(str(Path(npm).parent))
    if bins:
        prefix = os.pathsep.join(dict.fromkeys(bins))
        env["PATH"] = prefix + os.pathsep + env.get("PATH", "")
    return env


def resolve_node() -> str | None:
    """Prefer global ``node`` on PATH, then common install dirs, then optional env override.

    Do not require UIFORGEMAX_NODE in MCP config — only use it if the user explicitly set it
    after PATH + Program Files lookup failed (or as a last-resort override when set).
    """
    found = which_tool("node", "node.exe")
    if found:
        return found
    for candidate in _windows_node_candidates():
        if candidate.exists():
            return str(candidate)
    env = os.environ.get("UIFORGEMAX_NODE") or os.environ.get("NODE_BINARY")
    if env and Path(env).exists():
        return env
    return None


def resolve_npm() -> str | None:
    """Prefer global ``npm`` on PATH, then sibling of resolved node, then Program Files, then env."""
    found = which_tool("npm.cmd", "npm", "npm.exe")
    if found:
        return found
    node = resolve_node()
    if node:
        sibling = Path(node).parent / ("npm.cmd" if os.name == "nt" else "npm")
        if sibling.exists():
            return str(sibling)
    for candidate in _windows_npm_candidates():
        if candidate.exists():
            return str(candidate)
    env = os.environ.get("UIFORGEMAX_NPM") or os.environ.get("NPM_BINARY")
    if env and Path(env).exists():
        return env
    return None


def resolve_python_command() -> str | None:
    """Prefer ``python`` / ``python3`` / ``py`` on PATH (not a hardcoded MCP env path)."""
    return which_tool("python", "python3", "py")


def _windows_node_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "nodejs",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs",
        Path(os.environ.get("APPDATA", "")) / "nvm",
        Path.home() / "AppData" / "Roaming" / "nvm",
    ]
    out: list[Path] = []
    for root in roots:
        if not root:
            continue
        out.append(root / "node.exe")
        # nvm version folders
        if root.name == "nvm" and root.exists():
            for child in sorted(root.glob("v*/node.exe"), reverse=True):
                out.append(child)
    return out


def _windows_npm_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    out: list[Path] = []
    for node in _windows_node_candidates():
        out.append(node.parent / "npm.cmd")
        out.append(node.parent / "npx.cmd")
    return out


def find_js_workspace_root(start: Path) -> Path | None:
    """Best install root: lockfile > npm/pnpm workspaces root > nearest package.json."""
    cur = start.resolve()
    with_pkg: list[Path] = []
    for _ in range(8):
        if (cur / "package.json").exists():
            with_pkg.append(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    if not with_pkg:
        return None
    for c in with_pkg:
        if any(
            (c / name).exists()
            for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "npm-shrinkwrap.json")
        ):
            return c
    for c in with_pkg:
        try:
            meta = json.loads((c / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if meta.get("workspaces") or (meta.get("packages") and (c / "pnpm-workspace.yaml").exists()):
            return c
    return with_pkg[0]


def package_installed(workspace: Path, name: str) -> bool:
    """True if package exists under workspace/node_modules (hoist-aware at that root)."""
    base = workspace / "node_modules" / name
    return base.is_dir() or (base.with_suffix(".js")).exists() or (workspace / "node_modules" / ".bin" / name).exists()


def js_deps_ready(workspace: Path, *, need_packages: list[str] | None = None) -> bool:
    """True if node_modules looks usable; when need_packages set, those must exist here."""
    nm = workspace / "node_modules"
    if not nm.is_dir():
        return False
    for pkg in need_packages or []:
        if not package_installed(workspace, pkg):
            return False
    if need_packages:
        return True
    # Prefer evidence of a real install over empty stub folders
    if (nm / ".package-lock.json").exists() or (nm / ".modules.yaml").exists():
        return True
    try:
        next(nm.iterdir())
        return True
    except StopIteration:
        return False


def runner_packages_for_stack(stack: dict[str, Any] | None) -> list[str]:
    fw = str((stack or {}).get("testFramework") or "").lower()
    _KNOWN = {
        "vitest": ["vitest"],
        "jest": ["jest"],
        "mocha": ["mocha"],
        "jasmine": ["jasmine"],
        "pytest": [],
        "unittest": [],
        "junit": [],
        "testng": [],
        "xunit": [],
        "nunit": [],
        "go test": [],
        "cargo test": [],
        "rspec": ["rspec"],
        "minitest": [],
        "phpunit": ["phpunit"],
    }
    for name, pkgs in _KNOWN.items():
        if name in fw:
            return pkgs
    return []


def collect_toolchain_facts(
    roots: dict[str, Path],
    generated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Facts for TEST_ENV_RECOVERY mediation — model decides install/run from this, not guesses."""
    generated = generated or {}
    stack = generated.get("stack") or {}
    need_pkgs = runner_packages_for_stack(stack)
    node = resolve_node()
    npm = resolve_npm()
    root_facts: dict[str, Any] = {}
    for name, root in roots.items():
        root = root.resolve()
        install_root = find_js_workspace_root(root)
        if install_root and install_root != root:
            install_rel = os.path.relpath(install_root, root).replace("\\", "/")
        elif install_root:
            install_rel = "."
        else:
            install_rel = None

        runner_at_root = all(package_installed(root, p) for p in need_pkgs) if need_pkgs else js_deps_ready(root)
        runner_at_install = (
            all(package_installed(install_root, p) for p in need_pkgs)
            if install_root and need_pkgs
            else (js_deps_ready(install_root) if install_root else False)
        )
        need_install = bool(install_root) and not runner_at_install
        runner_path = None
        if need_pkgs and install_root:
            for p in need_pkgs:
                for cand in (
                    install_root / "node_modules" / p / f"{p}.mjs",
                    install_root / "node_modules" / p / "vitest.mjs",
                    install_root / "node_modules" / p / "bin" / f"{p}.js",
                ):
                    if cand.exists():
                        runner_path = str(cand)
                        break
        root_facts[name] = {
            "path": str(root),
            "jsInstallRoot": str(install_root) if install_root else None,
            "installRootRel": install_rel,
            "nodeModulesAtRoot": (root / "node_modules").is_dir(),
            "nodeModulesAtInstallRoot": bool(install_root and (install_root / "node_modules").is_dir()),
            "needPackages": need_pkgs,
            "runnerAtRoot": runner_at_root,
            "runnerAtInstallRoot": runner_at_install,
            "hoistedRunner": bool(runner_at_install and not runner_at_root),
            "runnerPath": runner_path,
            "needInstall": need_install,
            "avoidLocalRunnerPath": (
                [f"./node_modules/{p}" for p in need_pkgs]
                if runner_at_install and not runner_at_root
                else []
            ),
            "suggestedInstall": (
                {
                    "command": "npm install" if npm else "npm install",
                    "cwd": install_rel or ".",
                    "root": name,
                    "purpose": f"Install deps at JS workspace root so {need_pkgs or ['packages']} resolve",
                }
                if need_install
                else None
            ),
            "note": (
                "Packages may be hoisted to jsInstallRoot — do not assume ./node_modules/<runner> "
                "under the app root. Prefer installHints at installRootRel, then npm exec / npx from app, "
                "or node <runnerPath> with cwd at install root."
                if install_root and install_root != root
                else "Use installHints when needInstall is true; empty installHints is invalid then."
            ),
        }

    any_need = any(r.get("needInstall") for r in root_facts.values())
    # Playwright availability (UI stacks) — package may be hoisted at install root
    for name, rf in root_facts.items():
        install_root = Path(rf["jsInstallRoot"]) if rf.get("jsInstallRoot") else None
        app_root = Path(rf["path"])
        pw = playwright_package_present(install_root or app_root) or playwright_package_present(app_root)
        rf["playwrightPackagePresent"] = pw
        rf["suggestedPlaywrightInstall"] = (
            None
            if pw
            else {
                "command": "npm install -D @playwright/test && npx playwright install chromium",
                "cwd": rf.get("installRootRel") or ".",
                "root": name,
                "purpose": "One-shot Playwright + Chromium for UI e2e (optional soft suite)",
            }
        )

    missing_tools: list[dict[str, str]] = []
    if not node:
        missing_tools.append(
            {
                "tool": "node",
                "askUser": (
                    "Node.js not found on PATH or under Program Files. Ask the human for the "
                    "node executable path (or to install Node and ensure `node` is on PATH). "
                    "Optional override: UIFORGEMAX_NODE — do not invent a path."
                ),
            }
        )
    if not npm:
        missing_tools.append(
            {
                "tool": "npm",
                "askUser": (
                    "npm not found on PATH or beside node. Ask the human for npm.cmd path "
                    "(or fix PATH). Optional override: UIFORGEMAX_NPM — do not invent a path."
                ),
            }
        )

    return {
        "node": node,
        "npm": npm,
        "nodeFound": bool(node),
        "npmFound": bool(npm),
        "pythonCommand": resolve_python_command(),
        "discoveryOrder": "PATH command → Program Files / common dirs → optional env override → ask user",
        "missingTools": missing_tools,
        "stack": stack,
        "roots": root_facts,
        "needInstallAny": any_need,
        "mediationRules": [
            "You decide install + run steps — MCP only executes allowlisted installHints and your run[].",
            "Tool discovery is PATH-first (node/npm/python commands), then Program Files; "
            "do not require hard-coded tool paths in MCP env. If missingTools is non-empty, "
            "ask the human for that tool's path or installation — do not guess.",
            "If needInstallAny or any root.needInstall is true, installHints[] is REQUIRED (unless skipTests).",
            "Copy suggestedInstall when present; adjust only if this stack needs a different package manager.",
            "run[] must use a runner that exists after install (npm exec vitest, or node runnerPath).",
            "Never claim node_modules is ready when runnerAtInstallRoot is false.",
            "If hoistedRunner is true, do NOT run node ./node_modules/<runner> under the app cwd.",
            "For UI tickets with visual ACs: include appropriate DOM/component tests for the detected "
            "framework (Testing Library for React, ComponentFixture for Angular, @vue/test-utils for Vue, "
            "etc.) AND Playwright e2e when visual proof is needed. Mark Playwright run[] with "
            "suite='playwright' (optional). MCP tries one project-local install; if Playwright stays "
            "unavailable, those commands are soft-skipped — unit/DOM still decide pass.",
        ],
    }


def playwright_package_present(workspace: Path | None) -> bool:
    if workspace is None:
        return False
    return package_installed(workspace, "@playwright/test") or package_installed(workspace, "playwright")


def ensure_playwright(
    workspace: Path,
    *,
    attempt_marker: Path | None = None,
) -> dict[str, Any]:
    """One-shot install of @playwright/test + Chromium into the JS workspace.

    If already present, returns available=True. A second call after a failed/attempted
    install (marker file) does not retry — caller soft-skips Playwright suite.
    """
    ws = find_js_workspace_root(workspace) or workspace.resolve()
    if playwright_package_present(ws):
        return {
            "available": True,
            "skipped": True,
            "reason": "@playwright/test already present",
            "cwd": str(ws),
        }

    if attempt_marker is not None and attempt_marker.exists():
        return {
            "available": False,
            "skipped": True,
            "reason": "Playwright install already attempted once for this run — not available",
            "cwd": str(ws),
        }

    npm = resolve_npm()
    if not npm:
        status = {
            "available": False,
            "ok": False,
            "reason": "npm not found — cannot install Playwright",
            "cwd": str(ws),
        }
        if attempt_marker is not None:
            attempt_marker.parent.mkdir(parents=True, exist_ok=True)
            attempt_marker.write_text(json.dumps(status, indent=2), encoding="utf-8")
        return status

    steps: list[dict[str, Any]] = []
    # 1) package
    cmd1 = f'"{npm}" install -D @playwright/test'
    try:
        proc1 = run_with_timeout(
            cmd1,
            cwd=ws,
            timeout=install_timeout_seconds(),
            env=subprocess_env(),
        )
        steps.append(
            {
                "command": cmd1,
                "ok": proc1.returncode == 0 and not proc1.timed_out,
                "code": proc1.returncode,
                "timedOut": proc1.timed_out,
                "output": _snip((proc1.stdout or "") + "\n" + (proc1.stderr or "")),
            }
        )
    except FileNotFoundError as exc:
        steps.append({"command": cmd1, "ok": False, "reason": str(exc)})

    pkg_ok = playwright_package_present(ws) or any(s.get("ok") for s in steps)
    # 2) browser binary (Chromium only — lighter than full playwright install)
    if pkg_ok:
        npx = str(Path(npm).with_name("npx.cmd" if npm.lower().endswith(".cmd") else "npx"))
        cmd2 = (
            f'"{npx}" playwright install chromium'
            if Path(npx).exists()
            else f'"{npm}" exec -- playwright install chromium'
        )
        try:
            proc2 = run_with_timeout(
                cmd2,
                cwd=ws,
                timeout=int(os.environ.get("UIFORGEMAX_PLAYWRIGHT_INSTALL_TIMEOUT", "180")),
                env=subprocess_env(),
            )
            steps.append(
                {
                    "command": cmd2,
                    "ok": proc2.returncode == 0 and not proc2.timed_out,
                    "code": proc2.returncode,
                    "timedOut": proc2.timed_out,
                    "output": _snip((proc2.stdout or "") + "\n" + (proc2.stderr or "")),
                }
            )
            browsers_ok = proc2.returncode == 0 and not proc2.timed_out
        except FileNotFoundError as exc:
            steps.append({"command": cmd2, "ok": False, "reason": str(exc)})
            browsers_ok = False
    else:
        browsers_ok = False

    available = playwright_package_present(ws) and browsers_ok
    status = {
        "available": available,
        "ok": available,
        "cwd": str(ws),
        "steps": steps,
        "reason": (
            "Playwright + Chromium ready"
            if available
            else "Playwright install failed or incomplete — UI e2e soft-skipped"
        ),
    }
    if attempt_marker is not None:
        attempt_marker.parent.mkdir(parents=True, exist_ok=True)
        attempt_marker.write_text(json.dumps(status, indent=2), encoding="utf-8")
    return status


def _quote_path(path: str) -> str:
    return f'"{path}"' if " " in path or "\\" in path or "/" in path else path


def rewrite_js_command(cmd: str) -> str:
    """Prefix npm/npx with absolute paths when PATH is empty in the MCP shell."""
    npm = resolve_npm()
    node = resolve_node()
    c = cmd.strip()
    if not c:
        return c

    # Use callables for replacements so Windows paths (\\U...) are not re escape sequences.
    if npm:
        npx = str(Path(npm).with_name("npx.cmd" if npm.lower().endswith(".cmd") else "npx"))
        npm_q = _quote_path(npm)
        if Path(npx).exists():
            npx_q = _quote_path(npx)
            c = re.sub(r"(?i)^npx(\.cmd)?\b", lambda _m: npx_q, c, count=1)
        else:
            c = re.sub(r"(?i)^npx(\.cmd)?\s+", lambda _m: f"{npm_q} exec -- ", c, count=1)
        c = re.sub(r"(?i)^npm\.cmd\b", lambda _m: npm_q, c, count=1)
        c = re.sub(r"(?i)^npm\b", lambda _m: npm_q, c, count=1)

    if node and re.search(r"(?i)^node\b", c):
        node_q = _quote_path(node)
        c = re.sub(r"(?i)^node\b", lambda _m: node_q, c, count=1)

    return c


def _tool_install_allowed() -> bool:
    return os.environ.get("UIFORGEMAX_ALLOW_TOOL_INSTALL", "").lower() in ("1", "true", "yes")


def _auto_install_enabled() -> bool:
    return os.environ.get("UIFORGEMAX_AUTO_INSTALL", "1").lower() not in ("0", "false", "no")


SAFE_INSTALL_COMMANDS = {
    "npm install",
    "npm ci",
    "npm install -D @playwright/test",
    "npx playwright install chromium",
    "npm exec -- playwright install chromium",
    "poetry install",
    "pip install -r requirements.txt",
    "pnpm install",
    "yarn install",
}


@dataclass
class ToolStatus:
    available: bool
    installed_now: bool = False
    skipped: bool = False
    error: str | None = None
    details: dict[str, Any] | None = None


def smart_ensure_tool(
    tool_name: str,
    check_cmd: str,
    install_cmd: str | None,
    *,
    cwd: Path,
    timeout: int = 120,
    required: bool = False,
) -> ToolStatus:
    check = run_with_timeout(check_cmd, cwd=cwd, timeout=min(timeout, 30))
    if check.returncode == 0 and not check.timed_out:
        return ToolStatus(available=True, details={"check": check_cmd, "output": _snip(check.stdout)})

    if not _auto_install_enabled():
        msg = f"{tool_name} not found; auto-install disabled (UIFORGEMAX_AUTO_INSTALL=0)"
        if required:
            return ToolStatus(available=False, error=msg)
        return ToolStatus(available=False, skipped=True, error=msg)

    if install_cmd is None:
        msg = f"{tool_name} not found and no install command provided"
        if required:
            return ToolStatus(available=False, error=msg)
        return ToolStatus(available=False, skipped=True, error=msg)

    base_cmd = re.sub(r'^"[^"]*"\s*', '', install_cmd).strip()
    if not any(base_cmd.startswith(safe) for safe in SAFE_INSTALL_COMMANDS):
        msg = f"Install command not in allowlist: {base_cmd}"
        if required:
            return ToolStatus(available=False, error=msg)
        return ToolStatus(available=False, skipped=True, error=msg)

    result = run_with_timeout(install_cmd, cwd=cwd, timeout=timeout, env=subprocess_env())
    if result.timed_out:
        msg = f"{tool_name} install timed out after {timeout}s"
        if required:
            return ToolStatus(available=False, error=msg, details={"install": install_cmd, "timedOut": True})
        return ToolStatus(available=False, skipped=True, error=msg)

    if result.returncode != 0:
        msg = f"{tool_name} install failed (exit {result.returncode})"
        if required:
            return ToolStatus(available=False, error=msg, details={"output": _snip(result.stderr)})
        return ToolStatus(available=False, skipped=True, error=msg)

    recheck = run_with_timeout(check_cmd, cwd=cwd, timeout=min(timeout, 30))
    if recheck.returncode == 0 and not recheck.timed_out:
        return ToolStatus(available=True, installed_now=True, details={"install": install_cmd})

    msg = f"{tool_name} installed but re-check failed"
    if required:
        return ToolStatus(available=False, error=msg)
    return ToolStatus(available=False, skipped=True, error=msg)


def ensure_node_runtime() -> dict[str, Any]:
    """
    If node/npm are missing, install Node.js LTS via winget (Windows) when allowed.
    Requires UIFORGEMAX_ALLOW_TOOL_INSTALL=1. Idempotent when already installed.
    """
    if resolve_npm() and resolve_node():
        return {"ok": True, "skipped": True, "reason": "node/npm already available"}

    if not _tool_install_allowed():
        return {
            "ok": False,
            "skipped": True,
            "reason": (
                "node/npm not found on PATH or Program Files — ask the human to install "
                "Node.js (so `node`/`npm` work in a terminal) or provide UIFORGEMAX_NODE / "
                "UIFORGEMAX_NPM paths. Optional auto winget: UIFORGEMAX_ALLOW_TOOL_INSTALL=1"
            ),
            "askUser": True,
        }

    if os.name != "nt":
        return {
            "ok": False,
            "skipped": True,
            "reason": "auto Node install is Windows/winget only — install Node.js manually",
        }

    winget = which_tool("winget", "winget.exe")
    if not winget:
        return {"ok": False, "reason": "winget not found — install Node.js manually"}

    # OpenJS.NodeJS.LTS is the stable winget id for Node LTS
    cmd = (
        f'"{winget}" install -e --id OpenJS.NodeJS.LTS '
        "--accept-package-agreements --accept-source-agreements --disable-interactivity"
    )
    try:
        proc = run_with_timeout(cmd, cwd=Path.cwd(), timeout=install_timeout_seconds())
        # Refresh resolve after install (new PATH may not be in this process)
        npm = resolve_npm()
        node = resolve_node()
        # winget often leaves binaries under Program Files even if PATH not updated
        if not node:
            for candidate in _windows_node_candidates():
                if candidate.exists():
                    node = str(candidate)
                    break
        if not npm and node:
            sibling = Path(node).parent / "npm.cmd"
            if sibling.exists():
                npm = str(sibling)
        ok = (proc.returncode == 0 and not proc.timed_out) or bool(npm and node)
        return {
            "ok": ok,
            "command": cmd,
            "code": proc.returncode,
            "timedOut": proc.timed_out,
            "node": node,
            "npm": npm,
            "output": _snip((proc.stdout or "") + "\n" + (proc.stderr or "")),
        }
    except FileNotFoundError as exc:
        return {"ok": False, "command": cmd, "reason": str(exc)}


def ensure_js_dependencies(
    roots: dict[str, Path],
    *,
    prefer_ci: bool = True,
    need_packages: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Install JS deps at each relevant workspace root if required packages are missing.

    Prefer mediation installHints when present; this is a safety net so tests are not
    blocked solely because the model omitted installHints.
    """
    log: list[dict[str, Any]] = []
    npm = resolve_npm()
    if not npm:
        runtime = ensure_node_runtime()
        log.append({"step": "ensure_node_runtime", **runtime})
        npm = runtime.get("npm") or resolve_npm()
        # Persist discovered paths for this process so rewrite_js_command works
        if runtime.get("node") and not os.environ.get("UIFORGEMAX_NODE"):
            os.environ["UIFORGEMAX_NODE"] = str(runtime["node"])
        if npm and not os.environ.get("UIFORGEMAX_NPM"):
            os.environ["UIFORGEMAX_NPM"] = str(npm)
    if not npm:
        log.append(
            {
                "ok": False,
                "skipped": True,
                "reason": (
                    "npm not found — install Node.js or set UIFORGEMAX_NPM / "
                    "UIFORGEMAX_ALLOW_TOOL_INSTALL=1 for auto winget install"
                ),
            }
        )
        return log

    seen: set[Path] = set()
    for root in roots.values():
        ws = find_js_workspace_root(root)
        if ws is None or ws in seen:
            continue
        seen.add(ws)
        if js_deps_ready(ws, need_packages=need_packages):
            log.append(
                {
                    "ok": True,
                    "cwd": str(ws),
                    "skipped": True,
                    "reason": "required packages already present",
                    "needPackages": need_packages or [],
                }
            )
            continue

        lock = (ws / "package-lock.json").exists() or (ws / "npm-shrinkwrap.json").exists()
        verb = "ci" if prefer_ci and lock else "install"
        cmd = f'"{npm}" {verb}'
        try:
            proc = run_with_timeout(cmd, cwd=ws, timeout=install_timeout_seconds(), env=subprocess_env())
            log.append(
                {
                    "command": cmd,
                    "ok": proc.returncode == 0 and not proc.timed_out,
                    "code": proc.returncode,
                    "timedOut": proc.timed_out,
                    "cwd": str(ws),
                    "output": _snip((proc.stdout or "") + "\n" + (proc.stderr or "")),
                }
            )
            if proc.timed_out:
                # Do not chain a retry after a hang — hand control back to the human.
                break
            # If ci failed (lock drift), one retry with install
            if proc.returncode != 0 and verb == "ci":
                cmd2 = f'"{npm}" install'
                proc2 = run_with_timeout(cmd2, cwd=ws, timeout=install_timeout_seconds(), env=subprocess_env())
                log.append(
                    {
                        "command": cmd2,
                        "ok": proc2.returncode == 0 and not proc2.timed_out,
                        "code": proc2.returncode,
                        "timedOut": proc2.timed_out,
                        "cwd": str(ws),
                        "output": _snip((proc2.stdout or "") + "\n" + (proc2.stderr or "")),
                    }
                )
                if proc2.timed_out:
                    break
        except FileNotFoundError as exc:
            log.append({"command": cmd, "ok": False, "cwd": str(ws), "reason": str(exc)})
    return log


def ensure_python_dependencies(roots: dict[str, Path]) -> list[dict[str, Any]]:
    """Best-effort poetry/pip install when pyproject/requirements present and no venv marker."""
    log: list[dict[str, Any]] = []
    for root in roots.values():
        root = root.resolve()
        if (root / "poetry.lock").exists() and which_tool("poetry"):
            if (root / ".venv").exists():
                log.append({"ok": True, "cwd": str(root), "skipped": True, "reason": ".venv present"})
                continue
            cmd = "poetry install"
            log.append(_run_install(cmd, root))
        elif (root / "requirements.txt").exists():
            # Don't create a venv automatically — pip install --user is too invasive.
            # Prefer mediation installHints; only run if UIFORGEMAX_AUTO_PIP=1
            if os.environ.get("UIFORGEMAX_AUTO_PIP", "").lower() not in ("1", "true", "yes"):
                log.append(
                    {
                        "ok": False,
                        "cwd": str(root),
                        "skipped": True,
                        "reason": "requirements.txt found — set UIFORGEMAX_AUTO_PIP=1 or use installHints",
                    }
                )
                continue
            py = which_tool("python", "python3", "py") or "python"
            log.append(_run_install(f'"{py}" -m pip install -r requirements.txt', root))
    return log


def ensure_dependencies_for_tests(
    roots: dict[str, Path],
    generated: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Install whatever the mediated stack implies before running tests."""
    generated = generated or {}
    stack = generated.get("stack") or {}
    build = str(stack.get("buildTool") or "").lower()
    lang = str(stack.get("language") or "").lower()
    run_blob = " ".join(
        (e if isinstance(e, str) else str(e.get("command") or "")) for e in (generated.get("run") or [])
    ).lower()

    log: list[dict[str, Any]] = []
    needs_js = any(
        x in build or x in run_blob or x in lang
        for x in ("npm", "pnpm", "yarn", "vitest", "jest", "node", "typescript", "javascript")
    ) or any((r / "package.json").exists() for r in roots.values())

    needs_py = any(x in build or x in run_blob or x in lang for x in ("poetry", "pip", "pytest", "python"))

    if needs_js or (not build and any(find_js_workspace_root(r) for r in roots.values())):
        log.extend(
            ensure_js_dependencies(roots, need_packages=runner_packages_for_stack(stack))
        )
    if needs_py:
        log.extend(ensure_python_dependencies(roots))
    return log


def _run_install(cmd: str, cwd: Path) -> dict[str, Any]:
    try:
        proc = run_with_timeout(cmd, cwd=cwd, timeout=install_timeout_seconds(), env=subprocess_env())
        return {
            "command": cmd,
            "ok": proc.returncode == 0 and not proc.timed_out,
            "code": proc.returncode,
            "timedOut": proc.timed_out,
            "cwd": str(cwd),
            "output": _snip((proc.stdout or "") + "\n" + (proc.stderr or "")),
        }
    except FileNotFoundError as exc:
        return {"command": cmd, "ok": False, "cwd": str(cwd), "reason": str(exc)}


def _snip(text: str, limit: int = 500) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
