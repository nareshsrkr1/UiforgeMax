"""TEST_ENV_RECOVERY — missing tools hand control back to IDE mediation."""

from __future__ import annotations

import json
from pathlib import Path

from uiforgemax.model_mediation.registry import MediationKind, pending_mediations
from uiforgemax.pipeline.testing import (
    apply_test_env_recovery,
    can_request_env_recovery,
    env_recovery_pending,
    is_safe_install_command,
    results_need_env_recovery,
    validate_test_env_recovery,
    write_env_gap,
)
from uiforgemax.state import Stage


def test_safe_install_allowlist():
    ok, _ = is_safe_install_command("npm ci")
    assert ok
    ok, _ = is_safe_install_command("pip install -r requirements.txt")
    assert ok
    ok, _ = is_safe_install_command("./mvnw -q dependency:resolve")
    assert ok
    ok, reason = is_safe_install_command("rm -rf /")
    assert not ok
    ok, reason = is_safe_install_command("winget install Maven.Maven")
    assert not ok
    assert "UIFORGEMAX_ALLOW_TOOL_INSTALL" in reason


def test_safe_install_system_when_env_set(monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_ALLOW_TOOL_INSTALL", "1")
    ok, _ = is_safe_install_command("winget install Maven.Maven")
    assert ok


def test_results_need_env_recovery_detects_tool_missing():
    assert results_need_env_recovery(
        {
            "passed": False,
            "failures": ["[tool_missing] test command not found: mvn -q test"],
            "commandsRan": False,
            "mediated": True,
        }
    )
    assert not results_need_env_recovery(
        {
            "passed": False,
            "failures": ["[test_failed] test command failed (1): npm test :: AssertionError"],
            "commandsRan": True,
            "mediated": True,
        }
    )


def test_write_env_gap_and_pending_mediation(tmp_path, monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_SKIP_MEDIATION", raising=False)
    run_dir = tmp_path / "run"
    (run_dir / "tests").mkdir(parents=True)
    results = {
        "passed": False,
        "failures": ["[tool_missing] 'mvn' is not recognized"],
        "stack": {"buildTool": "maven"},
        "mode": "mediated",
    }
    gap = write_env_gap(run_dir, results, {"run": [{"command": "mvn -q test"}]})
    assert gap["status"] == "needs_recovery"
    assert env_recovery_pending(run_dir)
    assert can_request_env_recovery(run_dir)

    # Fake completed TEST_GENERATION so only recovery is pending
    med = run_dir / "mediation"
    med.mkdir()
    (med / "10_test_TEST_GENERATION.response.json").write_text("{}", encoding="utf-8")

    state = type("S", (), {"flow": {}, "inputs": {}})()
    pending = pending_mediations(Stage.TEST, run_dir, state)
    kinds = [k for _, k in pending]
    assert MediationKind.TEST_ENV_RECOVERY in kinds


def test_apply_recovery_merges_run_and_skip(tmp_path):
    run_dir = tmp_path / "run"
    root = tmp_path / "proj"
    root.mkdir()
    (run_dir / "tests").mkdir(parents=True)
    (run_dir / "tests" / "generated-tests.json").write_text(
        json.dumps({"run": [{"command": "mvn -q test"}], "tests": []}),
        encoding="utf-8",
    )
    write_env_gap(
        run_dir,
        {"passed": False, "failures": ["[tool_missing] mvn"], "stack": {}},
        {"run": [{"command": "mvn -q test"}]},
    )
    log = apply_test_env_recovery(
        root,
        run_dir,
        {
            "diagnosis": "use wrapper",
            "run": [{"command": "./mvnw -q test", "cwd": "."}],
            "installHints": [{"command": "rm -rf /", "purpose": "should skip"}],
        },
    )
    data = json.loads((run_dir / "tests" / "generated-tests.json").read_text(encoding="utf-8"))
    assert data["run"][0]["command"] == "./mvnw -q test"
    assert log["installs"][0]["skipped"] is True
    assert not env_recovery_pending(run_dir)
    # One attempt used; a second recovery is still allowed (MAX=2)
    assert can_request_env_recovery(run_dir)


def test_validate_recovery_requires_install_hints_when_facts_say_so(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "tests").mkdir(parents=True)
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"name":"app"}', encoding="utf-8")
    mono = tmp_path
    (mono / "package.json").write_text('{"workspaces":["app"]}', encoding="utf-8")
    # Point facts at missing vitest
    facts = {
        "needInstallAny": True,
        "roots": {
            "default": {
                "needInstall": True,
                "suggestedInstall": {"command": "npm install", "cwd": "..", "root": "default"},
                "avoidLocalRunnerPath": ["./node_modules/vitest"],
                "jsInstallRoot": str(mono),
                "runnerPath": None,
            }
        },
    }
    (run_dir / "tests" / "toolchain-facts.json").write_text(json.dumps(facts), encoding="utf-8")
    ok, reason = validate_test_env_recovery(
        run_dir,
        {"run": [{"command": "npm test"}], "installHints": []},
    )
    assert not ok
    assert "installHints" in reason

    ok, _ = validate_test_env_recovery(
        run_dir,
        {
            "run": [{"command": "npm exec -- vitest run"}],
            "installHints": [{"command": "npm install", "cwd": ".."}],
        },
    )
    assert ok

    ok, reason = validate_test_env_recovery(
        run_dir,
        {
            "run": [{"command": 'node ./node_modules/vitest/vitest.mjs run'}],
            "installHints": [{"command": "npm install", "cwd": ".."}],
        },
    )
    assert not ok
    assert "hoisted" in reason.lower() or "node_modules/vitest" in reason
