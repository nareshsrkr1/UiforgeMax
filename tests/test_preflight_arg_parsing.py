"""preflight/start_run JSON args accept both a real list/dict and a JSON string."""

from __future__ import annotations

from uiforgemax.tools.preflight import _parse_json_array, _parse_json_object


def test_array_accepts_real_list():
    # The agent commonly passes ["C:/path"] directly (not a JSON string).
    vals, err = _parse_json_array(["c:/Users/x/App"])
    assert err is None
    assert vals == ["c:/Users/x/App"]


def test_array_accepts_json_string():
    vals, err = _parse_json_array('["c:/Users/x/App"]')
    assert err is None
    assert vals == ["c:/Users/x/App"]


def test_array_none_and_empty():
    assert _parse_json_array(None) == ([], None)
    assert _parse_json_array("") == ([], None)


def test_array_bad_string_reports_error():
    vals, err = _parse_json_array("not json")
    assert vals == []
    assert err


def test_object_accepts_real_dict():
    vals, err = _parse_json_object({"ui": "c:/ui", "backend": "c:/be"})
    assert err is None
    assert vals == {"ui": "c:/ui", "backend": "c:/be"}


def test_object_accepts_json_string():
    vals, err = _parse_json_object('{"ui": "c:/ui"}')
    assert err is None
    assert vals == {"ui": "c:/ui"}
