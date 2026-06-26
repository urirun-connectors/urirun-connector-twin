"""URI dispatch shim for the twin connector.

Every capability in this connector has two call paths:
  - INTERNAL: direct Python function call (zero overhead, works standalone)
  - EXTERNAL:  HTTP POST to a served twin:// route via v2_service.call

`uri_call(uri, payload)` selects the path:
  - If a `_registry` has been injected (connector runs inside a served node),
    it forwards to the served route — so a remote twin node handles the call.
  - Otherwise it falls back to the internal Python implementation.

This means `plan_from_prompt_route` doesn't need to import `build_imperative_plan`
directly: it calls `twin://host/plan/command/generate` and the dispatch layer
decides whether to cross the network, stay in-process via the served registry,
or call the raw Python function.

The same mechanism enables hot-swapping: once a `twin` node is deployed with a
better `plan/command/generate` implementation, all callers pick it up without
changing their code — they still call the same URI.

Usage in route handlers:
    from ._dispatch import uri_call, set_registry

    result = uri_call("twin://host/environment/query/profile", {})
    env = result.get("value") or result

Injection (called once when the connector is served inside a node):
    set_registry(node_registry)
"""
from __future__ import annotations

from typing import Callable

_registry: dict | None = None
_transport_fn: Callable[[str, dict], dict] | None = None


def set_registry(registry: dict) -> None:
    """Inject the served node registry so URI calls use the live routes."""
    global _registry
    _registry = registry


def set_transport(fn: Callable[[str, dict], dict]) -> None:
    """Inject a custom transport (e.g. NodeClient.run) for cross-node calls."""
    global _transport_fn
    _transport_fn = fn


def _result_value(envelope: dict) -> dict:
    """Extract .result.value from a v2_service envelope, or return the envelope itself."""
    r = envelope.get("result")
    if isinstance(r, dict):
        v = r.get("value")
        return v if isinstance(v, dict) else r
    return envelope


def uri_call(uri: str, payload: dict | None = None) -> dict:
    """Call a URI, routing through the injected registry/transport or in-process fallback.

    Return value is always the *inner* result dict (not the v2_service envelope),
    so callers don't need to unwrap .result.value."""
    payload = payload or {}

    # 1. External transport (NodeClient or custom fn) — cross-node call
    if _transport_fn is not None:
        return _result_value(_transport_fn(uri, payload))

    # 2. Served registry in the same process — dispatch through the node's surface
    if _registry is not None:
        try:
            from urirun import runtime as _rt
            v2 = _safe_import("urirun.runtime.v2")
            if v2 is not None:
                result = v2.run(uri, _registry, payload, mode="execute")
                return _result_value(result)
        except Exception:
            pass  # fall through to in-process

    # 3. In-process dispatch via v2_service (talks to a node over HTTP)
    v2_svc = _safe_import("urirun.v2_service")
    if v2_svc is not None:
        try:
            result = v2_svc.call(uri, payload, mode="execute")
            if result.get("ok"):
                return _result_value(result)
        except Exception:
            pass

    # 4. Hard in-process fallback: call the Python function directly
    return _fallback(uri, payload)


def _safe_import(module: str):
    import importlib.util
    if importlib.util.find_spec(module) is None:
        return None
    try:
        return __import__(module, fromlist=[""])
    except Exception:
        return None


# ─── in-process fallback table ───────────────────────────────────────────────
# Maps URI suffix patterns to (module, function) so uri_call can call the
# Python implementation when no node/registry is available.

_FALLBACK_TABLE: list[tuple[str, str, str]] = [
    # (uri_contains, module, function)
    ("environment/query/profile",    ".environment",  "probe"),
    ("browser/query/sessions",       ".session",      "discover_browser_sessions"),
    ("browser/query/profile",        ".session",      "select_best_session"),
    ("session/query/discover",       ".session",      "discover_browser_sessions"),
    ("plan/command/generate",        ".planner",      "build_imperative_plan"),
    ("plan/command/from-prompt",     ".prompt_plan",  "plan_from_prompt"),
    ("step/query/feasibility",       ".planner",      "annotate_steps"),
    ("mock/command/create",          ".mock",         "generate_mock"),
]


def _fallback(uri: str, payload: dict) -> dict:
    """Resolve the URI to a local Python function and call it with payload as kwargs."""
    import importlib
    package = __name__.rsplit(".", 1)[0]  # urirun_connector_twin

    for pattern, rel_module, fn_name in _FALLBACK_TABLE:
        if pattern not in uri:
            continue
        try:
            mod = importlib.import_module(rel_module, package=package)
            fn = getattr(mod, fn_name)
            # spread payload as kwargs (matching handler signatures)
            result = fn(**{k: v for k, v in payload.items()
                           if not k.startswith("_")})
            if isinstance(result, dict):
                return result
            return {"value": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "uri": uri}

    return {"ok": False, "error": f"no fallback registered for {uri}"}
