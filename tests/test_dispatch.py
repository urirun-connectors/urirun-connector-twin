"""Tests for dispatch.py — URI-as-internal-bus switching layer with transport injection."""
from __future__ import annotations

import pytest
import urirun_connector_twin.dispatch as _d


# ─── helpers ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_transport():
    _d._transport_fn = None
    yield
    _d._transport_fn = None


# ─── basic dispatch.py behaviour ────────────────────────────────────────────

def test_uri_call_fallback_triggers_on_no_mesh():
    """When no mesh/transport, falls through to in-process fallback."""
    result = _d.uri_call("twin://host/not-a-real-route", {}, fallback=lambda: {"ok": True, "x": 9})
    assert result == {"ok": True, "x": 9}


def test_uri_call_fallback_none_returns_none():
    assert _d.uri_call("twin://host/missing", {}) is None


def test_value_of_extracts_value_key():
    assert _d.value_of({"ok": True, "value": {"a": 1}}) == {"a": 1}


def test_value_of_extracts_nested_result_value():
    assert _d.value_of({"result": {"value": {"b": 2}}}) == {"b": 2}


def test_value_of_none_input():
    assert _d.value_of(None) is None


def test_value_of_no_value_key():
    assert _d.value_of({"ok": True, "data": 1}) is None


# ─── transport injection ─────────────────────────────────────────────────────

def test_transport_is_called_first():
    """set_transport() makes the injected fn the primary dispatch path."""
    called = []

    def fake(uri, payload):
        called.append(uri)
        return {"ok": True, "result": {"value": {"source": "transport"}}}

    _d.set_transport(fake)
    result = _d.uri_call("twin://host/environment/query/profile", {})
    assert called == ["twin://host/environment/query/profile"]
    assert result is not None


def test_transport_result_not_ok_falls_to_fallback():
    """If transport returns ok=False, dispatch continues to v2_service / fallback."""
    _d.set_transport(lambda u, p: {"ok": False, "error": "not found"})
    sentinel = {"ok": True, "sentinel": True}
    result = _d.uri_call("twin://host/x", {}, fallback=lambda: sentinel)
    assert result == sentinel


def test_transport_exception_falls_to_fallback():
    """Transport exceptions are swallowed and dispatch falls through."""
    _d.set_transport(lambda u, p: (_ for _ in ()).throw(RuntimeError("bang")))
    sentinel = {"fallback": True}
    result = _d.uri_call("twin://host/x", {}, fallback=lambda: sentinel)
    assert result == sentinel


def test_set_transport_none_clears():
    """Passing None clears the transport."""
    _d.set_transport(lambda u, p: {"ok": True})
    _d.set_transport(None)
    assert _d._transport_fn is None
    # Without transport, should return None (no v2_service, no fallback)
    assert _d.uri_call("twin://host/x") is None


# ─── plan_from_prompt_route routes through URI dispatch ──────────────────────

def test_plan_from_prompt_route_calls_environment_uri():
    """plan_from_prompt_route calls twin://host/environment/... via uri_call."""
    transport_calls = []

    def fake(uri, payload):
        transport_calls.append(uri)
        if "environment" in uri:
            return {"ok": True, "host": "laptop", "node": None,
                    "bestSurface": "cdp", "controllable": True,
                    "constraints": [], "warnings": [], "docker": True,
                    "sessionSelection": {}}
        if "browser" in uri:
            return {"ok": True, "selection": {"mode": "no-chrome"}}
        return {"ok": True}

    _d.set_transport(fake)
    from urirun_connector_twin.core import plan_from_prompt_route
    r = plan_from_prompt_route("take a screenshot", node="laptop", probe_browser=False)
    assert r["ok"] is True
    env_calls = [u for u in transport_calls if "environment" in u]
    assert len(env_calls) >= 1, f"environment URI not called; got: {transport_calls}"


def test_plan_from_prompt_route_calls_browser_uri_when_domain():
    """When prompt has a domain, plan_from_prompt_route calls browser/query/profile URI."""
    transport_calls = []

    def fake(uri, payload):
        transport_calls.append(uri)
        if "environment" in uri:
            return {"ok": True, "host": "laptop", "node": None,
                    "bestSurface": "cdp", "controllable": True,
                    "constraints": [], "warnings": [], "docker": True,
                    "sessionSelection": {}}
        if "browser" in uri:
            return {"ok": True, "selection": {"mode": "no-chrome"}}
        return {"ok": True}

    _d.set_transport(fake)
    from urirun_connector_twin.core import plan_from_prompt_route
    r = plan_from_prompt_route("post on linkedin", node="laptop", probe_browser=True)
    assert r["ok"] is True
    browser_calls = [u for u in transport_calls if "browser" in u]
    assert len(browser_calls) >= 1, f"browser URI not called; got: {transport_calls}"


def test_plan_from_prompt_route_fallback_when_no_transport():
    """Without transport, plan_from_prompt_route still works via in-process fallback."""
    from urirun_connector_twin.core import plan_from_prompt_route
    r = plan_from_prompt_route("take a screenshot", node="", probe_browser=False)
    assert r["ok"] is True
    assert "plan" in r

