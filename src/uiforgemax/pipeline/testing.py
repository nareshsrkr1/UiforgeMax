"""Test stage — mediation-driven, stack-agnostic.

IDE ``TEST_GENERATION`` decides framework, test file paths, and shell commands
(Java/Maven, Gradle, pytest, npm/vitest, go test, etc.). MCP only:
1. writes the mediated test files
2. runs the mediated commands
3. applies cheap static/compliance checks from the plan

There is no hardcoded assumption that the project is Node/npm.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from uiforgemax.env_flags import install_timeout_seconds
from uiforgemax.graphify.stack import detect_stack
from uiforgemax.pipeline.implement import resolve_root
from uiforgemax.pipeline.toolchain import (
    collect_toolchain_facts,
    ensure_dependencies_for_tests,
    ensure_playwright,
    find_js_workspace_root,
    rewrite_js_command,
    run_with_timeout,
    subprocess_env,
)

# Frontend stacks that should get a production build check in TEST.
_UI_STACK_KINDS = frozenset({"react", "angular", "vue", "svelte"})
_UI_BUILD_TIMEOUT_SEC = 300

MAX_FIX_ATTEMPTS = 2
INSTALL_WAIT_PATH = "tests/install-wait.json"

_TEMPLATE_SANITY: dict[str, str] = {
    "greenfield.backend_main": "FastAPI",
}


def _normalize_roots(project_root: Path | dict[str, Path]) -> dict[str, Path]:
    if isinstance(project_root, dict):
        return project_root
    return {"default": project_root}


def load_generated_tests(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "tests" / "generated-tests.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def apply_generated_tests(project_root: Path | dict[str, Path], run_dir: Path) -> int:
    """Write IDE-mediated test files into the workspace before running tests."""
    data = load_generated_tests(run_dir)
    roots = _normalize_roots(project_root)
    written = 0
    for spec in data.get("tests", []) or []:
        rel = spec.get("path")
        content = spec.get("content")
        if not rel or content is None:
            continue
        target = resolve_root(roots, spec) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1
    return written


def _read_package_scripts(pkg_json: Path) -> dict[str, Any]:
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = data.get("scripts") if isinstance(data, dict) else None
    return scripts if isinstance(scripts, dict) else {}


def _is_ui_frontend_root(root: Path) -> bool:
    """True when this root looks like a React/Angular/Vue/Svelte (or similar) UI app."""
    stack = detect_stack(root)
    kinds = {str(k).lower() for k in (stack.get("kinds") or [])}
    if kinds & _UI_STACK_KINDS:
        return True
    # Strong markers even if detect_stack missed (sparse trees / tmp fixtures).
    if (root / "angular.json").exists():
        return True
    for name in ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs"):
        if (root / name).exists() and (
            any(root.glob("*.tsx"))
            or any(root.glob("*.jsx"))
            or (root / "index.html").exists()
            or (root / "src" / "index.html").exists()
        ):
            return True
    return False


def resolve_ui_build_command(root: Path) -> dict[str, Any] | None:
    """Pick one production build command for a UI root, or None to skip.

    Preference: ``package.json`` ``scripts.build`` → ``npm run build``;
    else Angular CLI; else Vite. Never invent a build for non-UI / API-only roots.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    ws = find_js_workspace_root(root) or root
    if not _is_ui_frontend_root(root) and not _is_ui_frontend_root(ws):
        return None

    pkg = ws / "package.json" if (ws / "package.json").exists() else root / "package.json"
    scripts = _read_package_scripts(pkg) if pkg.exists() else {}
    if "build" in scripts:
        return {
            "command": "npm run build",
            "cwd": str(ws.resolve()),
            "rootPath": str(root.resolve()),
            "reason": "package.json scripts.build",
        }

    angular = ws / "angular.json" if (ws / "angular.json").exists() else root / "angular.json"
    if angular.exists():
        return {
            "command": "npx ng build",
            "cwd": str(ws.resolve()),
            "rootPath": str(root.resolve()),
            "reason": "angular.json (no scripts.build)",
        }

    for name in ("vite.config.ts", "vite.config.js", "vite.config.mts", "vite.config.mjs"):
        if (ws / name).exists() or (root / name).exists():
            return {
                "command": "npx vite build",
                "cwd": str(ws.resolve()),
                "rootPath": str(root.resolve()),
                "reason": f"{name} (no scripts.build)",
            }
    return None


def _mediated_already_has_build(generated: dict[str, Any]) -> bool:
    """Skip MCP auto-build when TEST_GENERATION already emitted a build suite/command."""
    for entry in generated.get("run") or []:
        if not isinstance(entry, dict):
            continue
        suite = str(entry.get("suite") or entry.get("kind") or "").lower()
        cmd = str(entry.get("command") or "").lower()
        if suite == "build":
            return True
        if any(
            marker in cmd
            for marker in (
                "npm run build",
                "pnpm run build",
                "yarn build",
                "ng build",
                "vite build",
                "nx build",
            )
        ):
            return True
    return False


def run_ui_build_checks(
    roots: dict[str, Path],
    generated: dict[str, Any] | None = None,
    *,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    """Run one production build per UI root (React/Angular/Vue/…).

    Hard-fails the TEST stage when the build exits non-zero. Skips when no UI
    stack / no known build command, or when mediation already included suite=build.
    """
    generated = generated or {}
    status: dict[str, Any] = {
        "requested": False,
        "skipped": False,
        "passed": True,
        "commands": [],
        "failures": [],
    }
    if generated.get("skipTests") or generated.get("skipUiBuild"):
        status["skipped"] = True
        status["reason"] = "skipTests or skipUiBuild"
        return status
    if _mediated_already_has_build(generated):
        status["skipped"] = True
        status["reason"] = "mediated run[] already includes a build command"
        return status

    seen_cwds: set[str] = set()
    for name, root in roots.items():
        spec = resolve_ui_build_command(root)
        if not spec:
            continue
        cwd = str(spec["cwd"])
        if cwd in seen_cwds:
            continue
        seen_cwds.add(cwd)
        status["requested"] = True
        cmd = rewrite_js_command(str(spec["command"]))
        entry: dict[str, Any] = {
            "root": name,
            "command": cmd,
            "cwd": cwd,
            "reason": spec.get("reason"),
        }
        try:
            proc = run_with_timeout(
                cmd,
                cwd=cwd,
                env=subprocess_env(),
                timeout=_UI_BUILD_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 — surface as build failure
            entry["ok"] = False
            entry["error"] = str(exc)[:500]
            status["commands"].append(entry)
            status["passed"] = False
            status["failures"].append(f"[build] {name}: {cmd} — {exc}")
            continue

        entry["returncode"] = getattr(proc, "returncode", None)
        entry["timedOut"] = bool(getattr(proc, "timed_out", False))
        out = (getattr(proc, "stdout", "") or "")[-2000:]
        err = (getattr(proc, "stderr", "") or "")[-2000:]
        entry["stdoutTail"] = out
        entry["stderrTail"] = err
        if entry["timedOut"]:
            entry["ok"] = False
            status["passed"] = False
            status["failures"].append(
                f"[build] {name}: timed out after {_UI_BUILD_TIMEOUT_SEC}s — {cmd}"
            )
        elif entry["returncode"] != 0:
            entry["ok"] = False
            status["passed"] = False
            snippet = (err or out or "no output").strip().splitlines()
            tail = " | ".join(snippet[-3:])[:400]
            status["failures"].append(
                f"[build] {name}: {cmd} exited {entry['returncode']} — {tail}"
            )
        else:
            entry["ok"] = True
        status["commands"].append(entry)

    if not status["requested"]:
        status["skipped"] = True
        status["reason"] = "no UI frontend root with a known build command"

    if run_dir is not None:
        (run_dir / "tests").mkdir(parents=True, exist_ok=True)
        (run_dir / "tests" / "ui-build-status.json").write_text(
            json.dumps(status, indent=2), encoding="utf-8"
        )
    return status


def run_tests(
    project_root: Path | dict[str, Path],
    plan: dict[str, Any],
    run_dir: Path | None = None,
    *,
    skip_auto_install: bool = False,
) -> dict[str, Any]:
    roots = _normalize_roots(project_root)
    generated = load_generated_tests(run_dir) if run_dir else {}
    results: dict[str, Any] = {
        "passed": True,
        "attempts": [],
        "failures": [],
        "coverage": {},
        "stack": generated.get("stack") or {},
        "mediated": bool(generated.get("run") or generated.get("tests")),
        "skippedTests": False,
        "installs": [],
        "playwright": {},
        "uiBuild": {},
        "skippedAutoInstall": skip_auto_install,
    }

    # Mediation installHints first, then safety-net ensure when packages still missing.
    # After a prior install timeout, skip auto-install — human was asked to install locally.
    if (
        not skip_auto_install
        and (generated.get("run") or generated.get("tests") or generated.get("installHints"))
    ):
        install_log: list[dict[str, Any]] = []
        if generated.get("installHints"):
            install_log.extend(run_allowlisted_installs(roots, generated.get("installHints") or []))
        timed_out = [e for e in install_log if e.get("timedOut")]
        if timed_out:
            # Do not chain ensure_* installs (another long hang). Hand back to the human.
            results["installs"] = install_log
            results["installTimedOut"] = True
            results["humanInstallRequired"] = True
            results["passed"] = False
            timeout_sec = install_timeout_seconds()
            cmds = [e.get("command") for e in timed_out]
            cwds = list({e.get("cwd") for e in timed_out if e.get("cwd")})
            results["failures"] = [
                (
                    f"[timeout] dependency install exceeded {timeout_sec}s and was stopped. "
                    f"Commands: {cmds}. cwd={cwds}. "
                    "Install dependencies yourself in a terminal, then call uiforgemax_advance "
                    "to retry tests (auto-install will be skipped on retry). "
                    f"Override wait with UIFORGEMAX_INSTALL_TIMEOUT (seconds)."
                )
            ]
            if run_dir is not None:
                (run_dir / "tests").mkdir(parents=True, exist_ok=True)
                (run_dir / "tests" / "install-log.json").write_text(
                    json.dumps(install_log, indent=2), encoding="utf-8"
                )
                write_toolchain_facts(run_dir, roots, generated)
                default_root = roots.get("default")
                write_install_wait(
                    run_dir,
                    install_log,
                    timeout_sec,
                    project_root=str(default_root) if default_root else None,
                    run_id=run_dir.name,
                )
            return results
        install_log.extend(ensure_dependencies_for_tests(roots, generated))
        results["installs"] = install_log
        if run_dir is not None:
            (run_dir / "tests").mkdir(parents=True, exist_ok=True)
            (run_dir / "tests" / "install-log.json").write_text(
                json.dumps(install_log, indent=2), encoding="utf-8"
            )
            write_toolchain_facts(run_dir, roots, generated)

    if generated.get("skipTests"):
        # Still run static file/compliance checks; skip mediated command execution.
        attempt_result = _run_once(
            roots, plan, {**generated, "run": []}, skip_commands=True, run_dir=run_dir
        )
        results["attempts"].append(attempt_result)
        results["skippedTests"] = True
        results["skipReason"] = generated.get("skipReason")
        results["passed"] = attempt_result["ok"]
        results["failures"] = attempt_result.get("failures", [])
        results["coverage"] = attempt_result.get("coverage") or {}
        results["playwright"] = attempt_result.get("playwright") or {}
        return results

    # React/Angular/Vue/Svelte: prove `npm run build` (or ng/vite) after deps, before unit/e2e.
    # Only when TEST mediation is active (installs/tests/run) so we don't blind-build API-only.
    if (
        generated.get("run")
        or generated.get("tests")
        or generated.get("installHints")
    ) and not generated.get("skipUiBuild"):
        ui_build = run_ui_build_checks(roots, generated, run_dir=run_dir)
        results["uiBuild"] = ui_build
        if ui_build.get("failures"):
            results["failures"] = list(ui_build["failures"])
            if not ui_build.get("passed", True):
                results["passed"] = False
                # Fail-fast: a broken production build is enough to stop TEST.
                return results

    for attempt in range(1, MAX_FIX_ATTEMPTS + 2):
        attempt_result = _run_once(roots, plan, generated, run_dir=run_dir)
        results["attempts"].append(attempt_result)
        if attempt_result.get("coverage"):
            results["coverage"] = attempt_result["coverage"]
        if attempt_result.get("playwright"):
            results["playwright"] = attempt_result["playwright"]
        if attempt_result["ok"]:
            results["passed"] = True
            break
        results["passed"] = False
        results["failures"] = attempt_result.get("failures", [])
        if attempt > MAX_FIX_ATTEMPTS:
            break
        _apply_fixes(roots, plan, attempt_result.get("failures", []))

    if run_dir is not None and results.get("playwright"):
        (run_dir / "tests" / "playwright-status.json").write_text(
            json.dumps(results["playwright"], indent=2), encoding="utf-8"
        )

    return results


def _run_once(
    roots: dict[str, Path],
    plan: dict[str, Any] | None,
    generated: dict[str, Any] | None,
    *,
    skip_commands: bool = False,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    plan = plan or {}
    generated = generated or {}
    created = plan.get("create", []) or []
    modified = plan.get("modify", []) or []
    all_files = created + modified
    validation = plan.get("validationPlan") or {}
    automated = validation.get("automated") or (plan.get("tests") or {}).get("automated") or {}
    cov_cfg = generated.get("coverage") or automated.get("coverageThreshold") or {}

    if not all_files and not generated.get("tests"):
        return {"ok": True, "failures": [], "mode": "no-plan", "coverage": {}}

    failures: list[str] = []
    failures.extend(_missing_file_checks(roots, created))
    failures.extend(_template_sanity_checks(roots, created))
    failures.extend(_compliance_checks(roots, plan, all_files))
    # Prefer mediated test paths; fall back to plan hints
    failures.extend(_unit_test_presence_checks(roots, plan, generated))

    ran, cmd_failures, coverage, playwright_status = False, [], {}, {}
    if not skip_commands:
        if generated.get("run"):
            ran, cmd_failures, coverage, playwright_status = _run_mediated_commands(
                roots, generated, cov_cfg, run_dir=run_dir
            )
            failures.extend(cmd_failures)
        elif generated.get("tests") and not generated.get("skipTests"):
            # Mediation wrote tests but forgot run[] — recover via TEST_ENV_RECOVERY
            failures.append(
                "[env] no run[] commands from TEST_GENERATION — "
                "IDE must specify how to run tests for this stack (pytest, vitest, mvn, …)"
            )
        # else: no mediation payload → static-only (SKIP_MEDIATION / scaffold paths)

    soft_cov = bool(cov_cfg.get("soft", True))
    threshold = int(cov_cfg.get("linesThreshold") or cov_cfg.get("lines") or 70)
    want_cov = bool(
        (generated.get("coverage") or {}).get("required", automated.get("codeCoverage", False))
    )
    if want_cov and coverage.get("lines") is not None and coverage["lines"] < threshold:
        msg = f"code coverage {coverage['lines']}% below threshold {threshold}%"
        failures.append(f"[soft] {msg}" if soft_cov else msg)

    hard = [f for f in failures if not str(f).startswith("[soft]")]
    mode = "mediated" if generated.get("run") else ("discovered" if ran else "static-only")
    return {
        "ok": len(hard) == 0,
        "failures": failures,
        "mode": mode,
        "coverage": coverage,
        "commandsRan": ran,
        "playwright": playwright_status,
    }


def _unit_test_presence_checks(
    roots: dict[str, Path],
    plan: dict[str, Any],
    generated: dict[str, Any],
) -> list[str]:
    """Only verify paths the IDE mediation actually emitted — never plan framework hints."""
    unit_paths = [t["path"] for t in (generated.get("tests") or []) if t.get("path")]
    if not unit_paths:
        if generated.get("skipTests") or not generated:
            # No mediation payload (e.g. UIFORGEMAX_SKIP_MEDIATION) → static-only OK
            return []
        return [
            "[env] TEST_GENERATION produced no tests[] — "
            "IDE must generate stack-appropriate tests (pytest/vitest/junit/…)"
        ]
    require = os.environ.get("UIFORGEMAX_REQUIRE_UNIT_FILES", "").lower() in ("1", "true", "yes")
    missing = []
    for rel in unit_paths:
        found = any((root / rel).exists() for root in roots.values())
        if not found:
            missing.append(f"missing mediated test file: {rel}")
    if not missing:
        return []
    return missing if require else [f"[soft] {m}" for m in missing]


def _missing_file_checks(roots: dict[str, Path], created: list[dict[str, Any]]) -> list[str]:
    failures = []
    for f in created:
        rel = f.get("path")
        if rel and not (resolve_root(roots, f) / rel).exists():
            failures.append(f"missing created file: {rel}")
    return failures


def _template_sanity_checks(roots: dict[str, Path], created: list[dict[str, Any]]) -> list[str]:
    failures = []
    for f in created:
        needle = _TEMPLATE_SANITY.get(f.get("templateId", ""))
        if not needle:
            continue
        target = resolve_root(roots, f) / f.get("path", "")
        if target.exists() and needle not in target.read_text(encoding="utf-8", errors="ignore"):
            failures.append(f"{f['path']} missing expected marker: {needle!r}")
    return failures


def _compliance_checks(roots: dict[str, Path], plan: dict[str, Any], files: list[dict[str, Any]]) -> list[str]:
    tests = plan.get("tests")
    compliance = tests.get("compliance", {}) if isinstance(tests, dict) else {}
    exact_texts = compliance.get("exactTextRequirements") or []
    if not exact_texts:
        return []
    combined = ""
    for f in files:
        rel = f.get("path")
        target = resolve_root(roots, f) / rel if rel else None
        if target and target.exists():
            combined += target.read_text(encoding="utf-8", errors="ignore")
    return [f'exact text requirement not found: "{text}"' for text in exact_texts if text not in combined]


def _is_playwright_entry(entry: dict[str, Any], cmd: str) -> bool:
    suite = str(entry.get("suite") or entry.get("kind") or "").lower()
    if suite in ("playwright", "e2e", "browser"):
        return True
    if entry.get("optional") and "playwright" in cmd.lower():
        return True
    return "playwright" in cmd.lower()


def _append_command_log(run_dir: Path | None, entry: dict[str, Any]) -> None:
    """Persist full command output for TEST_ENV_RECOVERY (snips hide the real error)."""
    if run_dir is None:
        return
    path = run_dir / "tests" / "command-logs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, json.JSONDecodeError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    # Cap stored output so artifacts stay readable but keep the FAIL/Error tail.
    out = entry.get("output") or ""
    if len(out) > 20000:
        entry = {**entry, "output": out[-20000:], "outputTruncated": True}
    existing.append(entry)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def _run_mediated_commands(
    roots: dict[str, Path],
    generated: dict[str, Any],
    cov_cfg: dict[str, Any],
    *,
    run_dir: Path | None = None,
) -> tuple[bool, list[str], dict[str, Any], dict[str, Any]]:
    """Execute IDE-provided run commands — primary path for all stacks.

    Playwright / browser suite commands are optional: MCP tries one project-local
    install; if Playwright is still unavailable they are soft-skipped (status in
    return value / tests/playwright-status.json) and do not fail the run.
    """
    commands = list(generated.get("run") or [])
    if not commands:
        return False, [], {}, {}

    ran = False
    failures: list[str] = []
    coverage: dict[str, Any] = {}
    summary_glob = cov_cfg.get("summaryGlob")
    playwright_status: dict[str, Any] = {"requested": False, "available": None, "ran": False}
    playwright_ready: bool | None = None

    for entry in commands:
        if isinstance(entry, str):
            entry = {"command": entry, "cwd": ".", "root": "default"}
        cmd = (entry.get("command") or "").strip()
        if not cmd:
            continue
        root = roots.get(entry.get("root") or "default") or roots.get("default")
        if root is None:
            failures.append(f"[env] no root for command: {cmd}")
            continue
        cwd = (root / (entry.get("cwd") or ".")).resolve()
        if not cwd.exists():
            failures.append(f"[env] cwd missing for '{cmd}': {cwd}")
            continue

        if _is_playwright_entry(entry, cmd):
            playwright_status["requested"] = True
            if playwright_ready is None:
                marker = (
                    (run_dir / "tests" / "playwright-install-attempt.json") if run_dir else None
                )
                ws = find_js_workspace_root(root) or root
                pw = ensure_playwright(ws, attempt_marker=marker)
                playwright_ready = bool(pw.get("available"))
                playwright_status.update(
                    {
                        "available": playwright_ready,
                        "install": pw,
                        "reason": pw.get("reason"),
                    }
                )
            if not playwright_ready:
                playwright_status.setdefault("skippedCommands", []).append(cmd)
                playwright_status["status"] = "unavailable"
                # Soft note only — unit/DOM suite still decides pass/fail
                failures.append(
                    f"[soft] Playwright unavailable — skipped: {cmd} "
                    f"({playwright_status.get('reason') or 'install failed'})"
                )
                continue

        # Resolve npm/npx/node to absolute paths — MCP PATH is often incomplete on Windows.
        cmd = rewrite_js_command(cmd)
        try:
            timeout = int(
                os.environ.get(
                    "UIFORGEMAX_PLAYWRIGHT_TIMEOUT"
                    if _is_playwright_entry(entry, cmd)
                    else "UIFORGEMAX_TEST_TIMEOUT",
                    "300" if _is_playwright_entry(entry, cmd) else "180",
                )
            )
            proc = run_with_timeout(cmd, cwd=cwd, timeout=timeout, env=subprocess_env())
            if proc.timed_out:
                _append_command_log(
                    run_dir,
                    {
                        "command": cmd,
                        "cwd": str(cwd),
                        "returncode": proc.returncode,
                        "timedOut": True,
                        "output": ((proc.stdout or "") + "\n" + (proc.stderr or ""))[-20000:],
                    },
                )
                if _is_playwright_entry(entry, cmd) and not entry.get("required", False):
                    failures.append(f"[soft] Playwright timed out: {cmd}")
                else:
                    failures.append(f"[timeout] test command timed out: {cmd}")
                continue
            ran = True
            if _is_playwright_entry(entry, cmd):
                playwright_status["ran"] = True
                playwright_status["status"] = "ran" if proc.returncode == 0 else "failed"
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            _append_command_log(
                run_dir,
                {
                    "command": cmd,
                    "cwd": str(cwd),
                    "returncode": proc.returncode,
                    "timedOut": False,
                    "output": out[-20000:],
                },
            )
            cov = _parse_coverage(cwd, out, summary_glob)
            if cov:
                coverage = cov
            if proc.returncode != 0:
                kind = _classify_command_failure(cmd, out, proc.returncode)
                # Playwright failures are hard only when mediation set required=true
                if _is_playwright_entry(entry, cmd) and not entry.get("required", False):
                    failures.append(
                        f"[soft] Playwright test failed ({proc.returncode}): {cmd} :: "
                        f"{_snip_test_output(out)}"
                    )
                else:
                    failures.append(
                        f"[{kind}] test command failed ({proc.returncode}): {cmd} :: "
                        f"{_snip_test_output(out)}"
                    )
        except FileNotFoundError:
            _append_command_log(
                run_dir,
                {"command": cmd, "cwd": str(cwd), "returncode": None, "error": "FileNotFoundError"},
            )
            if _is_playwright_entry(entry, cmd) and not entry.get("required", False):
                failures.append(f"[soft] Playwright command not found: {cmd}")
                playwright_status["status"] = "unavailable"
            else:
                failures.append(f"[tool_missing] test command not found: {cmd}")

    if playwright_status.get("requested") and playwright_status.get("status") is None:
        playwright_status["status"] = (
            "available" if playwright_status.get("available") else "unavailable"
        )

    return ran, failures, coverage, playwright_status


_TOOL_MISSING_MARKERS = (
    "is not recognized as an internal or external command",
    "command not found",
    "no such file or directory",
    "unable to find",
    "could not find",
    "no module named",
    "modulenotfounderror",
    "npm err! missing",
    "error: cannot find module",
    "cannot find module",
    "cannot find dependency",
    "missing dependency",
    "could not resolve",
    "err_module_not_found",
    "'mvn' is not recognized",
    "'gradle' is not recognized",
    "'pytest' is not recognized",
    "program not found",
)

# Assertion / suite noise that must NOT be treated as a missing-tool env gap.
_ASSERTION_MARKERS = (
    "assertionerror",
    "expect(",
    "expected ",
    "received ",
    "referenceerror: expect is not defined",
    "failed suites",
    "✓",
    "×",
    "fail ",
)


def _looks_like_missing_dep(blob: str) -> bool:
    """True for missing packages/modules across JS/Python/etc. (not assertion noise)."""
    b = (blob or "").lower()
    if any(m in b for m in _TOOL_MISSING_MARKERS):
        return True
    if re.search(r"cannot find module ['\"][^'\"]+['\"]", b):
        return True
    if "modulenotfounderror" in b or "err_module_not_found" in b:
        return True
    return False


def _classify_command_failure(cmd: str, output: str, code: int) -> str:
    blob = (output or "").lower()
    # Missing modules/peers win even when vitest prints a FAIL summary around them.
    if _looks_like_missing_dep(blob):
        return "tool_missing"
    # Prefer test_failed when vitest/jest clearly ran assertions (avoid "not found" false positives).
    if any(m in blob for m in _ASSERTION_MARKERS) or "failed suites" in blob:
        return "test_failed"
    # Bare "not found" / "not recognized" only when not an assertion report
    if "not recognized" in blob or re.search(r"\bnot found\b", blob):
        return "tool_missing"
    if code in (127, 9009):  # unix not found / Windows command not found
        return "tool_missing"
    # Opaque exit-1 with empty capture — treat as env gap so mediation can re-run / install.
    if code != 0 and not (output or "").strip():
        return "env"
    return "test_failed"


def _snip(text: str, limit: int = 240) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _snip_test_output(text: str, limit: int = 1200) -> str:
    """Prefer the FAIL / Error tail — head-only snips hide the real assertion."""
    raw = text or ""
    compact = " ".join(raw.split())
    if len(compact) <= limit:
        return compact
    lower = raw.lower()
    for marker in ("failed suites", "\nfail ", "referenceerror", "assertionerror", "error:"):
        idx = lower.rfind(marker)
        if idx >= 0:
            chunk = " ".join(raw[idx:].split())
            return chunk if len(chunk) <= limit else chunk[: limit - 3] + "..."
    return compact[-limit:]


# --- Env-gap recovery (TEST_ENV_RECOVERY mediation) -------------------------------

MAX_ENV_RECOVERY_ATTEMPTS = 2
ENV_GAP_PATH = "tests/env-gap.json"
TOOLCHAIN_FACTS_PATH = "tests/toolchain-facts.json"


def env_gap_path(run_dir: Path) -> Path:
    return run_dir / ENV_GAP_PATH


def load_env_gap(run_dir: Path) -> dict[str, Any]:
    path = env_gap_path(run_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def env_recovery_pending(run_dir: Path) -> bool:
    """True when MCP wrote an env gap and IDE has not answered yet."""
    gap = load_env_gap(run_dir)
    return gap.get("status") == "needs_recovery"


def results_need_env_recovery(results: dict[str, Any]) -> bool:
    """Hard failures that look like missing tools / bad env (not assertion failures)."""
    if results.get("passed"):
        return False
    for f in results.get("failures") or []:
        s = str(f).lower()
        if s.startswith("[soft"):
            continue
        if "[tool_missing]" in s or "[env]" in s or "[timeout]" in s:
            return True
        if _looks_like_missing_dep(s):
            return True
    # No run commands produced and mediation expected tests → env/strategy gap
    if not results.get("commandsRan") and results.get("mediated"):
        hard = [f for f in (results.get("failures") or []) if not str(f).startswith("[soft")]
        if hard:
            return True
    return False


def can_request_env_recovery(run_dir: Path) -> bool:
    gap = load_env_gap(run_dir)
    if gap.get("status") == "needs_recovery":
        return True
    attempts = int(gap.get("attempt") or 0)
    return attempts < MAX_ENV_RECOVERY_ATTEMPTS


def write_toolchain_facts(
    run_dir: Path,
    roots: dict[str, Path],
    generated: dict[str, Any] | None = None,
) -> dict[str, Any]:
    facts = collect_toolchain_facts(roots, generated)
    path = run_dir / TOOLCHAIN_FACTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    return facts


def load_toolchain_facts(run_dir: Path) -> dict[str, Any]:
    path = run_dir / TOOLCHAIN_FACTS_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_env_gap(
    run_dir: Path,
    results: dict[str, Any],
    generated: dict[str, Any],
    *,
    roots: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Persist diagnosis for TEST_ENV_RECOVERY mediation."""
    prev = load_env_gap(run_dir)
    if prev.get("status") == "needs_recovery":
        return prev
    attempt = int(prev.get("attempt") or 0) + 1
    facts: dict[str, Any] = {}
    if roots:
        facts = write_toolchain_facts(run_dir, roots, generated)
    # Allow a second recovery attempt: drop the prior IDE response so mediation pauses again.
    if attempt > 1:
        resp = run_dir / "mediation" / "10_test_TEST_ENV_RECOVERY.response.json"
        resp.unlink(missing_ok=True)
        req = run_dir / "mediation" / "10_test_TEST_ENV_RECOVERY.request.json"
        req.unlink(missing_ok=True)
    missing: list[str] = []
    for rf in (facts.get("roots") or {}).values():
        missing.extend(rf.get("missingPackages") or [])
    gap = {
        "status": "needs_recovery",
        "attempt": attempt,
        "maxAttempts": MAX_ENV_RECOVERY_ATTEMPTS,
        "stack": results.get("stack") or generated.get("stack") or {},
        "failures": results.get("failures") or [],
        "priorRun": generated.get("run") or [],
        "mode": results.get("mode"),
        "toolchainFacts": "tests/toolchain-facts.json",
        "commandLogs": "tests/command-logs.json",
        "needInstallAny": bool(facts.get("needInstallAny")),
        "missingPackages": sorted(set(missing)),
        "note": (
            "IDE must decide recovery from tests/toolchain-facts.json + tests/command-logs.json. "
            "Cannot find module / missingPackages → installHints for those packages (any stack: "
            "npm/pnpm/pip/mvn/…). Prefer install + corrected run[] over skipTests. "
            "Empty installHints while needInstallAny/missingPackages is rejected."
        ),
    }
    path = env_gap_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(gap, indent=2), encoding="utf-8")
    return gap


def validate_test_generation(run_dir: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """Reject empty or unjustified TEST_GENERATION payloads for UI work."""
    tests = payload.get("tests") or []
    skip = bool(payload.get("skipTests"))
    if skip:
        if not (payload.get("skipReason") or "").strip():
            return False, "skipTests requires skipReason"
        return True, ""
    if not tests:
        # Allow empty only for pure api_only surfaces with no UI files changed.
        surface = "unknown"
        cls_path = run_dir / "request-classification.json"
        if cls_path.exists():
            try:
                surface = str(json.loads(cls_path.read_text(encoding="utf-8")).get("surface") or "unknown")
            except (OSError, json.JSONDecodeError):
                pass
        if surface in ("api_only", "infra"):
            return True, ""
        return (
            False,
            "tests[] is empty — emit real unit/DOM tests (or skipTests with skipReason). "
            "Read inputs/page.html / visual-spec.json and assert SoT labels where relevant.",
        )
    # Reject stub-only bodies (tiny files with no assert/expect)
    stubby = 0
    for t in tests:
        content = str(t.get("content") or "")
        path = str(t.get("path") or "")
        if not path or not content.strip():
            return False, f"each tests[] entry needs path + content (bad: {path or '(no path)'})"
        lower = content.lower()
        has_assert = any(
            token in lower
            for token in (
                "expect(", "assert", "tobe(", "toequal(", "@test",
                # Go: t.Errorf/t.Fatal/t.Fatalf/t.Run rarely contain "assert" at all —
                # idiomatic Go tests use plain if-checks + t.Errorf, not an assert lib.
                "t.error", "t.fatal", "t.run(",
                # Go/Ruby/etc. testify-/rspec-style require./should.
                "require.", "should.", "should_",
                # Generic BDD declaration wrappers (weak signal, backed by the
                # existing length check so a truly empty it()/test() still fails).
                "it(", "test(", "describe(",
            )
        )
        if len(content.strip()) < 80 or not has_assert:
            stubby += 1
    if stubby == len(tests):
        return (
            False,
            "tests[] look like empty stubs — include real assertions "
            "(expect/assert) against implemented UI or SoT labels.",
        )
    run = payload.get("run") or []
    if tests and not run:
        return False, "tests[] present but run[] missing — specify how to execute them"
    return True, ""


def validate_test_env_recovery(run_dir: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    """Reject recovery that ignores toolchain facts (e.g. skipped install when needed)."""
    facts = load_toolchain_facts(run_dir)
    gap = load_env_gap(run_dir)
    hints = payload.get("installHints") or []
    run = payload.get("run") or []
    missing: list[str] = list(gap.get("missingPackages") or [])
    for rf in (facts.get("roots") or {}).values():
        missing.extend(rf.get("missingPackages") or [])
    missing = sorted({m for m in missing if m})
    fail_blob = " ".join(str(f) for f in (gap.get("failures") or [])).lower()
    module_gap = bool(missing) or _looks_like_missing_dep(fail_blob)

    if payload.get("skipTests"):
        if not (payload.get("skipReason") or "").strip():
            return False, "skipTests requires skipReason"
        # Do not allow skip as the first response to a clear missing-package failure.
        if module_gap and not hints:
            return (
                False,
                "failures look like missing packages/modules "
                f"(missingPackages={missing or 'see command-logs'}). "
                "Return installHints[] for those packages (e.g. npm install -D @testing-library/dom) "
                "and a run[] retry — do not skipTests until install was attempted.",
            )
        return True, ""

    if not run and not hints:
        return False, "provide run[] and/or installHints[], or skipTests"

    if (facts.get("needInstallAny") or missing) and not hints:
        suggested = []
        for rf in (facts.get("roots") or {}).values():
            if rf.get("suggestedInstall"):
                suggested.append(rf["suggestedInstall"])
        return (
            False,
            "toolchain-facts.json needInstallAny/missingPackages — installHints[] is required "
            f"(missing={missing}; suggested: {json.dumps(suggested)}). "
            "MCP executes those installs; do not skip them.",
        )

    # Reject app-local vitest paths when runner is hoisted
    for rf in (facts.get("roots") or {}).values():
        avoid = rf.get("avoidLocalRunnerPath") or []
        if not avoid:
            continue
        for entry in run:
            cmd = entry if isinstance(entry, str) else str((entry or {}).get("command") or "")
            cmd_n = cmd.replace("\\", "/")
            for bad in avoid:
                if bad.replace("\\", "/") in cmd_n:
                    return (
                        False,
                        f"run[] uses '{bad}' but runner is hoisted at "
                        f"{rf.get('jsInstallRoot')} (runnerPath={rf.get('runnerPath')}). "
                        "Use npm exec / npx from app cwd, or node <runnerPath> with cwd at install root, "
                        "and include installHints if needInstall.",
                    )
    return True, ""


def mark_env_gap_applied(run_dir: Path, recovery: dict[str, Any]) -> None:
    gap = load_env_gap(run_dir)
    gap["status"] = "applied"
    gap["recovery"] = {
        "diagnosis": recovery.get("diagnosis"),
        "skipTests": bool(recovery.get("skipTests")),
        "installCount": len(recovery.get("installHints") or []),
        "runCount": len(recovery.get("run") or []),
    }
    env_gap_path(run_dir).write_text(json.dumps(gap, indent=2), encoding="utf-8")


def apply_test_env_recovery(
    project_root: Path | dict[str, Path],
    run_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Merge recovery into generated-tests.json and run allowlisted installs."""
    roots = _normalize_roots(project_root)
    generated = load_generated_tests(run_dir)
    if payload.get("stack"):
        generated["stack"] = payload["stack"]
    if payload.get("run") is not None:
        generated["run"] = payload["run"]
    if payload.get("coverage") is not None:
        generated["coverage"] = payload["coverage"]
    if payload.get("tests"):
        # Append/replace by path
        by_path = {t.get("path"): t for t in (generated.get("tests") or []) if t.get("path")}
        for t in payload["tests"]:
            if t.get("path"):
                by_path[t["path"]] = t
        generated["tests"] = list(by_path.values())
    if payload.get("skipTests"):
        generated["skipTests"] = True
        generated["skipReason"] = payload.get("skipReason") or "IDE skipped tests after env gap"
        generated["run"] = []

    out = run_dir / "tests" / "generated-tests.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(generated, indent=2), encoding="utf-8")

    # Write any new/updated test files from recovery
    written = apply_generated_tests(roots, run_dir)

    # Mediation installHints first (model decides), then safety-net ensure if still missing.
    install_log = list(run_allowlisted_installs(roots, payload.get("installHints") or []))
    install_log.extend(ensure_dependencies_for_tests(roots, generated))
    write_toolchain_facts(run_dir, roots, generated)
    mark_env_gap_applied(run_dir, payload)
    log = {
        "testsWritten": written,
        "installs": install_log,
        "skipTests": bool(payload.get("skipTests")),
    }
    (run_dir / "tests" / "recovery-log.json").write_text(json.dumps(log, indent=2), encoding="utf-8")
    return log


def write_install_wait(
    run_dir: Path,
    install_log: list[dict[str, Any]],
    timeout_sec: int,
    *,
    project_root: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Pause the run after install timeout — human finishes deps, then resume/advance."""
    timed = [e for e in install_log if e.get("timedOut") or e.get("ok") is False]
    commands = [
        {"command": e.get("command"), "cwd": e.get("cwd"), "timedOut": bool(e.get("timedOut"))}
        for e in timed
    ]
    # Prefer a single copy-pasteable shell block for the user.
    shell_lines: list[str] = []
    for c in commands:
        cwd = c.get("cwd") or project_root or "."
        cmd = c.get("command") or ""
        if cwd and cwd not in (".",):
            shell_lines.append(f'cd "{cwd}"')
        if cmd:
            shell_lines.append(cmd)
    if not shell_lines and project_root:
        # Stack-agnostic fallback — do not assume npm. Prefer mediated installHints.
        shell_lines = [
            f'cd "{project_root}"',
            "# Install deps for THIS stack (from tests/generated-tests.json installHints), then:",
            "# say resume / call uiforgemax_advance",
        ]
    shell_block = "\n".join(shell_lines)
    mins = max(1, int(timeout_sec) // 60)
    payload = {
        "status": "awaiting_user_install",
        "timeoutSec": timeout_sec,
        "timeoutMinutes": mins,
        "projectRoot": project_root,
        "runId": run_id,
        "resumeStage": "10_test",
        "resumeAction": "advance",
        "checkpointFile": "resume-checkpoint.json",
        "completedThrough": "9_implement",
        "pendingFrom": "10_test",
        "commands": commands,
        "shell": shell_block,
        "message": (
            f"Auto-install stopped after ~{mins} min ({timeout_sec}s). "
            "Implementation is already done — only dependency install/tests remain. "
            "Run the shell commands below in a terminal, then say **resume** "
            "(or call uiforgemax_resume_run / uiforgemax_advance). "
            "MCP will continue from tests without re-running a long install."
        ),
        "agentHint": (
            "On user 'resume' / 'done' / 'installed': call uiforgemax_resume_run "
            "(or uiforgemax_advance on this runId). Do NOT start a new run. "
            "nextTool is always uiforgemax_advance — it retries tests only."
        ),
    }
    path = run_dir / INSTALL_WAIT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Lightweight handover so humans know how to continue without hunting stages.
    handover_dir = run_dir / "handover"
    handover_dir.mkdir(parents=True, exist_ok=True)
    md = f"""# Install paused — resume from tests

Run `{run_id or run_dir.name}` paused during **dependency install** (timeout ~{mins} min).
Code from the approved plan is already written.

## Do this in a terminal

```bat
{shell_block}
```

## Then resume

In Cursor chat say **resume** (or **done** / **installed**).

The agent should call:

- `uiforgemax_resume_run(project_root='{project_root or ''}')` **or**
- `uiforgemax_advance(run_id='{run_id or run_dir.name}')`

MCP continues at stage **10_test** (tests + handover) — no new run, no re-plan.
"""
    (handover_dir / "install-paused.md").write_text(md, encoding="utf-8")
    (handover_dir / "install-paused.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def load_install_wait(run_dir: Path) -> dict[str, Any]:
    path = run_dir / INSTALL_WAIT_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ack_install_wait_for_retry(run_dir: Path) -> bool:
    """If human was asked to install, mark retry-tests-only and return True."""
    from datetime import datetime, timezone

    wait = load_install_wait(run_dir)
    if wait.get("status") != "awaiting_user_install":
        return False
    wait["status"] = "retrying_tests_only"
    wait["ackedAt"] = datetime.now(timezone.utc).isoformat()
    path = run_dir / INSTALL_WAIT_PATH
    path.write_text(json.dumps(wait, indent=2), encoding="utf-8")
    return True


def clear_install_wait(run_dir: Path) -> None:
    path = run_dir / INSTALL_WAIT_PATH
    path.unlink(missing_ok=True)


def run_allowlisted_installs(
    roots: dict[str, Path],
    hints: list[dict[str, Any] | str],
) -> list[dict[str, Any]]:
    """Run only safe dependency/tool install commands from mediation.

    Stops after the first timeout so the run can return to the human quickly.
    """
    log: list[dict[str, Any]] = []
    timeout_sec = install_timeout_seconds()
    for hint in hints:
        if isinstance(hint, str):
            hint = {"command": hint, "cwd": ".", "root": "default"}
        cmd = (hint.get("command") or "").strip()
        if not cmd:
            continue
        safe, reason = is_safe_install_command(cmd)
        if not safe:
            log.append({"command": cmd, "ok": False, "skipped": True, "reason": reason})
            continue
        root = roots.get(hint.get("root") or "default") or roots.get("default")
        if root is None:
            log.append({"command": cmd, "ok": False, "reason": "no root"})
            continue
        cwd = (root / (hint.get("cwd") or ".")).resolve()
        cmd = rewrite_js_command(cmd)
        try:
            proc = run_with_timeout(cmd, cwd=cwd, timeout=timeout_sec, env=subprocess_env())
            if proc.timed_out:
                log.append(
                    {
                        "command": cmd,
                        "ok": False,
                        "timedOut": True,
                        "cwd": str(cwd),
                        "timeoutSec": timeout_sec,
                        "reason": f"[timeout] install exceeded {timeout_sec}s",
                    }
                )
                # Do not start the next install hint — hand control back.
                break
            log.append(
                {
                    "command": cmd,
                    "ok": proc.returncode == 0,
                    "code": proc.returncode,
                    "cwd": str(cwd),
                    "timeoutSec": timeout_sec,
                    "output": _snip((proc.stdout or "") + (proc.stderr or ""), 400),
                }
            )
        except FileNotFoundError as exc:
            log.append({"command": cmd, "ok": False, "reason": str(exc), "cwd": str(cwd)})
    return log

def is_safe_install_command(cmd: str) -> tuple[bool, str]:
    """Allow project-local dependency installs; system package managers are gated."""
    c = " ".join(cmd.strip().split())
    lower = c.lower().strip('"').strip("'")
    # Strip quoted absolute npm paths: "C:\...\npm.cmd" ci
    lower_norm = re.sub(r'^"[^"]+"\s+', "", lower)
    lower_norm = re.sub(r"^'[^']+'\s+", "", lower_norm)
    check = lower_norm if lower_norm != lower else lower

    banned = (
        "rm -rf",
        "del /",
        "format ",
        "mkfs",
        "dd if=",
        "| sh",
        "| bash",
        "curl ",
        "wget ",
        "invoke-expression",
        "iex ",
        "remove-item -recurse",
    )
    if any(b in check for b in banned):
        return False, "command matches banned pattern"

    system_prefixes = (
        "winget ",
        "choco ",
        "chocolatey ",
        "brew ",
        "apt ",
        "apt-get ",
        "yum ",
        "sdkman ",
        "scoop ",
    )
    allow_system = os.environ.get("UIFORGEMAX_ALLOW_TOOL_INSTALL", "").lower() in ("1", "true", "yes")
    if any(check.startswith(p) for p in system_prefixes):
        if allow_system:
            return True, "system install allowed by UIFORGEMAX_ALLOW_TOOL_INSTALL"
        return False, "system package manager blocked (set UIFORGEMAX_ALLOW_TOOL_INSTALL=1)"

    # npm / npm.cmd / absolute …\npm.cmd  + ci|install|i
    if re.match(r"^(npm(\.cmd)?|pnpm(\.cmd)?|yarn(\.cmd)?)\s+(ci|install|i)\b", check):
        return True, "allowlisted js package install"
    if re.search(r"npm(\.cmd)?[\"']?\s+(ci|install|i)\b", check):
        return True, "allowlisted js package install (path-qualified)"

    # Playwright package + browser binaries (project-local; Chromium only preferred)
    if re.search(
        r"(npx(\.cmd)?\s+)?playwright\s+install(\s+(chromium|firefox|webkit))?\b",
        check,
    ):
        return True, "allowlisted playwright browser install"
    if "playwright install" in check:
        return True, "allowlisted playwright browser install"

    allowed_prefixes = (
        "pip install",
        "python -m pip install",
        "py -m pip install",
        "poetry install",
        "pipenv install",
        "mvn ",
        "./mvnw ",
        "mvnw ",
        "gradle ",
        "./gradlew ",
        "gradlew ",
        "go mod download",
        "go mod tidy",
        "dotnet restore",
        "dotnet tool restore",
        "bundle install",
        "composer install",
    )
    if any(check.startswith(p) for p in allowed_prefixes):
        if any(x in check for x in (" clean ", "deploy", "release:", "publish")):
            return False, "destructive build goal not allowlisted for installHints"
        return True, "allowlisted project install"
    return False, "command not in install allowlist"


def _parse_coverage(cwd: Path, output: str, summary_glob: str | None) -> dict[str, Any]:
    """Best-effort coverage from common report locations / stdout."""
    candidates = []
    if summary_glob:
        candidates.extend(cwd.glob(summary_glob))
    candidates.extend(
        [
            cwd / "coverage" / "coverage-summary.json",
            cwd / "target" / "site" / "jacoco" / "index.html",
            cwd / "build" / "reports" / "jacoco" / "test" / "html" / "index.html",
            cwd / "coverage.xml",
        ]
    )
    for summary in candidates:
        if not summary.exists():
            continue
        if summary.suffix == ".json":
            try:
                data = json.loads(summary.read_text(encoding="utf-8"))
                total = data.get("total") or {}
                lines = total.get("lines") or {}
                if "pct" in lines:
                    return {
                        "lines": lines["pct"],
                        "statements": (total.get("statements") or {}).get("pct"),
                        "branches": (total.get("branches") or {}).get("pct"),
                        "functions": (total.get("functions") or {}).get("pct"),
                        "source": str(summary),
                    }
            except (OSError, json.JSONDecodeError, TypeError):
                pass
        if summary.name == "index.html" or summary.suffix == ".xml":
            try:
                text = summary.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"Total[^%]*?([\d.]+)\s*%", text, re.I | re.S)
                if m:
                    return {"lines": float(m.group(1)), "source": str(summary)}
            except OSError:
                pass

    m = re.search(r"Lines\s*:\s*([\d.]+)\s*%", output, re.I)
    if m:
        return {"lines": float(m.group(1)), "source": "stdout"}
    m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)  # coverage.py
    if m:
        return {"lines": float(m.group(1)), "source": "stdout"}
    return {}


def _apply_fixes(roots: dict[str, Path], plan: dict[str, Any], failures: list[str]) -> None:
    if not failures:
        return
    hard = [f for f in failures if not str(f).startswith("[soft]")]
    if not hard:
        return
    from uiforgemax.pipeline.implement import apply_plan

    apply_plan(roots, plan)
