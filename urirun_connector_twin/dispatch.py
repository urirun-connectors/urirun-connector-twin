"""URI-first dispatcher: try the urirun mesh, fall back to a local callable.

Switching boundary that makes each capability replaceable:
  - If a transport fn has been injected (e.g. NodeClient.run), use that first.
  - If the URI is served via v2_service (local node), use the mesh result.
  - If not reachable, invoke the fallback() callable.

Basic usage:
    result = uri_call("kvm://host/env/query/profile", {}, fallback=local_fn)

Transport injection (for cross-node or testing):
    set_transport(node_client.run)   # NodeClient
    set_transport(my_stub)           # test stub

Swapping behaviour is a URI change, not an import change.
"""
from __future__ import annotations

from typing import Callable

_transport_fn: Callable[[str, dict], dict] | None = None


def set_transport(fn: Callable[[str, dict], dict] | None) -> None:
    """Inject a transport fn(uri, payload) -> dict used before v2_service.

    Pass None to clear.  Used by tests to stub URI calls and by the node
    runner to bind a NodeClient so calls cross the network automatically."""
    global _transport_fn
    _transport_fn = fn


def uri_call(
    uri: str,
    payload: dict | None = None,
    *,
    fallback: Callable[[], dict | None] | None = None,
    timeout: float = 5.0,
) -> dict | None:
    """Call a URI, routing through: transport → v2_service → fallback.

    Returns None when all paths produce no useful result."""
    payload = payload or {}

    # 1. Injected transport (NodeClient.run, test stub, cross-node)
    if _transport_fn is not None:
        try:
            r = _transport_fn(uri, payload)
            if r and r.get("ok") is not False:
                return r
        except Exception:
            pass  # fall through

    # 2. v2_service in-process (served node on localhost)
    try:
        from urirun.runtime import v2_service  # type: ignore[import]
        r = v2_service.call(uri, payload, timeout=timeout)
        if r and r.get("ok"):
            return r
    except Exception:
        pass

    # 3. Fallback (in-process Python implementation)
    return fallback() if fallback is not None else None


def value_of(result: dict | None, key: str = "value") -> dict | None:
    """Extract the nested value dict from a mesh result envelope."""
    if not result:
        return None
    v = result.get(key) or result.get("result", {}).get(key)
    return v if isinstance(v, dict) else None
