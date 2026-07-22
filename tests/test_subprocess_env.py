"""Graphify subprocesses must not inherit UiForgeMax's PYTHONPATH / env vars."""

from __future__ import annotations

from uiforgemax.graphify.cli_runner import _clean_subprocess_env


def test_strips_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", r"C:\Users\me\UiforgeMax\src")
    env = _clean_subprocess_env()
    assert "PYTHONPATH" not in env


def test_strips_uiforgemax_vars(monkeypatch):
    monkeypatch.setenv("UIFORGEMAX_DATA_ROOT", r"C:\data")
    monkeypatch.setenv("UIFORGEMAX_GRAPHIFY_UPDATE_TIMEOUT", "900")
    env = _clean_subprocess_env()
    assert not any(k.startswith("UIFORGEMAX_") for k in env)


def test_keeps_path_and_other_vars(monkeypatch):
    monkeypatch.setenv("PATH", r"C:\Windows;C:\Windows\System32")
    monkeypatch.setenv("SOME_OTHER_VAR", "keepme")
    env = _clean_subprocess_env()
    assert "PATH" in env
    assert env.get("SOME_OTHER_VAR") == "keepme"
