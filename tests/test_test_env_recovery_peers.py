"""Missing peer modules must trigger install mediation, not opaque skipTests."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.pipeline.testing import (
    _classify_command_failure,
    results_need_env_recovery,
    validate_test_env_recovery,
    write_env_gap,
)
from uiforgemax.pipeline.toolchain import (
    collect_toolchain_facts,
    packages_implied_by_generated_tests,
)


def test_classify_cannot_find_module_as_tool_missing():
    out = (
        "Error: Cannot find module '@testing-library/dom'\n"
        "Require stack:\n- node_modules/@testing-library/react/dist/pure.js\n"
        " FAIL  src/features/x.test.tsx\n"
        "Test Files  1 failed (1)\n"
    )
    assert _classify_command_failure("npm exec vitest run x.test.tsx", out, 1) == "tool_missing"


def test_classify_opaque_exit_as_env():
    assert _classify_command_failure("npm exec vitest run x", "", 1) == "env"


def test_results_need_env_recovery_for_module_gap():
    results = {
        "passed": False,
        "failures": [
            "[tool_missing] test command failed (1): npm exec vitest :: "
            "Error: Cannot find module '@testing-library/dom'"
        ],
    }
    assert results_need_env_recovery(results) is True


def test_packages_implied_includes_testing_library_peer():
    generated = {
        "tests": [
            {
                "path": "a.test.tsx",
                "content": (
                    "import { render } from '@testing-library/react';\n"
                    "import { describe, it } from 'vitest';\n"
                ),
            }
        ]
    }
    pkgs = packages_implied_by_generated_tests(generated)
    assert "@testing-library/react" in pkgs
    assert "@testing-library/dom" in pkgs
    assert "vitest" in pkgs


def test_toolchain_facts_marks_missing_peer(tmp_path: Path):
    app = tmp_path / "ui"
    (app / "node_modules" / "vitest").mkdir(parents=True)
    (app / "node_modules" / "@testing-library" / "react").mkdir(parents=True)
    # intentionally no @testing-library/dom
    generated = {
        "stack": {"language": "typescript", "testFramework": "vitest", "buildTool": "npm"},
        "tests": [
            {
                "path": "x.test.tsx",
                "content": "import { render } from '@testing-library/react';\n",
            }
        ],
    }
    facts = collect_toolchain_facts({"default": app}, generated)
    rf = facts["roots"]["default"]
    assert "@testing-library/dom" in rf["missingPackages"]
    assert facts["needInstallAny"] is True
    assert rf["suggestedInstall"] is not None
    assert "@testing-library/dom" in rf["suggestedInstall"]["command"]


def test_skip_tests_rejected_when_missing_module(tmp_path: Path):
    run_dir = tmp_path / "run"
    (run_dir / "tests").mkdir(parents=True)
    (run_dir / "tests" / "toolchain-facts.json").write_text(
        json.dumps(
            {
                "needInstallAny": True,
                "roots": {
                    "default": {
                        "missingPackages": ["@testing-library/dom"],
                        "needInstall": True,
                        "suggestedInstall": {
                            "command": "npm install -D @testing-library/dom",
                            "cwd": ".",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_env_gap(
        run_dir,
        {
            "passed": False,
            "failures": [
                "[tool_missing] Cannot find module '@testing-library/dom'"
            ],
            "stack": {"testFramework": "vitest"},
        },
        {"stack": {"testFramework": "vitest"}, "run": []},
    )
    ok, reason = validate_test_env_recovery(
        run_dir,
        {"skipTests": True, "skipReason": "opaque exit-1"},
    )
    assert ok is False
    assert "installHints" in reason

    ok2, _ = validate_test_env_recovery(
        run_dir,
        {
            "installHints": [
                {
                    "command": "npm install -D @testing-library/dom",
                    "cwd": ".",
                    "purpose": "peer dep",
                }
            ],
            "run": [{"command": "npm exec vitest run x.test.tsx", "cwd": "."}],
        },
    )
    assert ok2 is True
