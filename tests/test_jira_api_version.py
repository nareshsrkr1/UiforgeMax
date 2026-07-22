"""Jira REST API version selection: Cloud (v3) vs Server/Data Center (v2)."""

from __future__ import annotations

from typing import Any

import pytest

from uiforgemax.adapters.jira import fetch_issue
from uiforgemax.config import JiraConfig


class _FakeResponse:
    def __init__(self, url: str) -> None:
        self._url = url

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return {"key": "PROJ-1", "fields": {"summary": "s", "description": "", "labels": []}}


class _FakeClient:
    captured_urls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc) -> None:
        pass

    def get(self, url: str, **kwargs) -> _FakeResponse:
        _FakeClient.captured_urls.append(url)
        return _FakeResponse(url)


@pytest.fixture(autouse=True)
def _reset_captured():
    _FakeClient.captured_urls.clear()
    yield
    _FakeClient.captured_urls.clear()


def test_cloud_config_uses_api_v3(monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_USE_FIXTURES", raising=False)
    monkeypatch.setattr("uiforgemax.adapters.jira.httpx.Client", _FakeClient)
    config = JiraConfig(base_url="https://foo.atlassian.net", email="a@b.com", api_token="tok")
    fetch_issue("PROJ-1", config)
    assert "/rest/api/3/issue/PROJ-1" in _FakeClient.captured_urls[0]


def test_datacenter_config_uses_api_v2(monkeypatch):
    monkeypatch.delenv("UIFORGEMAX_USE_FIXTURES", raising=False)
    monkeypatch.setattr("uiforgemax.adapters.jira.httpx.Client", _FakeClient)
    config = JiraConfig(base_url="https://agile-jira.company.net", email=None, api_token="pat-token")
    assert config.uses_bearer
    fetch_issue("PROJ-1", config)
    assert "/rest/api/2/issue/PROJ-1" in _FakeClient.captured_urls[0]
