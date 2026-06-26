"""twin:// connector — environment probe + imperative plan generator with Docker mock fallback."""
from .core import CONNECTOR_ID, conn, bindings, manifest, main

__all__ = ["CONNECTOR_ID", "conn", "bindings", "manifest", "main"]
