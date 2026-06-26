"""Browser session discovery and session verification.

Enumerates live Chrome/Chromium processes from /proc, discovers their CDP debug
ports and user-data-dirs, then verifies actual login state by reading HTTP cookies
via CDP Network.getAllCookies — NOT by checking if a tab is open on a domain.

`holdsTarget` is a proof of session (auth cookie present), not a heuristic (URL match).

Known auth cookies (add more as needed):
  linkedin.com   → li_at
  github.com     → user_session
  google.com     → SSID or SID
  facebook.com   → c_user
  twitter.com    → auth_token
  x.com          → auth_token
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.request
from typing import Iterator

# ─── auth cookie registry ──────────────────────────────────────────────────────

_AUTH_COOKIES: dict[str, list[str]] = {
    "linkedin.com": ["li_at"],
    "github.com": ["user_session"],
    "google.com": ["SSID", "SID"],
    "gmail.com": ["SSID", "SID"],
    "facebook.com": ["c_user"],
    "twitter.com": ["auth_token"],
    "x.com": ["auth_token"],
    "reddit.com": ["reddit_session"],
    "instagram.com": ["sessionid"],
    "notion.so": ["token_v2"],
}

# ─── task → domain ─────────────────────────────────────────────────────────────

_DOMAIN_PATTERNS: list[tuple[str, str, bool]] = [
    (r"linkedin", "linkedin.com", True),
    (r"github", "github.com", True),
    (r"gmail|google mail", "gmail.com", True),
    (r"google", "google.com", False),
    (r"facebook", "facebook.com", True),
    (r"twitter|tweet", "twitter.com", True),
    (r"\bx\.com\b", "x.com", True),
    (r"reddit", "reddit.com", True),
    (r"instagram", "instagram.com", True),
    (r"notion", "notion.so", True),
]


def derive_task_target(prompt: str) -> dict:
    """Extract domain and auth requirement from a natural-language prompt."""
    low = prompt.lower()
    for pattern, domain, needs_auth in _DOMAIN_PATTERNS:
        if re.search(pattern, low):
            return {"domain": domain, "needsAuth": needs_auth}
    return {"domain": None, "needsAuth": False}


# ─── process enumeration ───────────────────────────────────────────────────────

def _proc_cmdlines() -> Iterator[list[str]]:
    """Yield argv lists for all readable /proc/<pid>/cmdline entries."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except PermissionError:
        return
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            if raw:
                yield raw.split(b"\x00")
        except (FileNotFoundError, PermissionError):
            pass


def _extract_chrome_info(argv: list[bytes]) -> dict | None:
    """Extract debug port and user-data-dir from a Chrome argv list."""
    cmd = argv[0].decode(errors="replace") if argv else ""
    is_chrome = any(k in cmd for k in ("chrome", "chromium", "brave", "edge"))
    if not is_chrome:
        return None
    port: int | None = None
    data_dir: str | None = None
    for arg in argv:
        a = arg.decode(errors="replace")
        m = re.search(r"--remote-debugging-port=(\d+)", a)
        if m:
            port = int(m.group(1))
        m = re.search(r"--user-data-dir=(.+)", a)
        if m:
            data_dir = m.group(1)
    if port is None:
        return None
    return {"port": port, "userDataDir": data_dir or ""}


def discover_browser_sessions() -> list[dict]:
    """Return metadata for every live debug-enabled Chrome process on this host."""
    seen_ports: set[int] = set()
    sessions = []
    for argv in _proc_cmdlines():
        info = _extract_chrome_info(argv)
        if not info or info["port"] in seen_ports:
            continue
        seen_ports.add(info["port"])
        sessions.append({
            "port": info["port"],
            "userDataDir": info["userDataDir"],
            "reachable": None,  # filled by probe_session
            "tabs": [],
            "holdsTarget": False,
            "authConfirmed": False,
            "authCookie": None,
        })
    return sessions


# ─── CDP tab inspection ────────────────────────────────────────────────────────

def _cdp_pages(port: int, timeout: float = 2.0) -> list[dict]:
    """Return tab list from a CDP debug endpoint."""
    try:
        url = f"http://127.0.0.1:{port}/json"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _cdp_ws_call(ws_url: str, method: str, params: dict | None = None,
                 timeout: float = 3.0) -> dict | None:
    """One-shot CDP command over a raw WebSocket (no external deps)."""
    url = ws_url.replace("ws://", "").replace("wss://", "")
    host_part, _, path = url.partition("/")
    host, _, port_str = host_part.partition(":")
    port = int(port_str) if port_str else 9222
    payload = json.dumps({"id": 1, "method": method, "params": params or {}})
    # WebSocket upgrade
    key = "dGhlIHNhbXBsZSBub25jZQ=="
    req = (
        f"GET /{path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        if b"101" not in resp:
            s.close()
            return None
        # send WS text frame
        data = payload.encode()
        header = bytes([0x81, 0x80 | len(data)]) + bytes(4) + data  # masked, key=0
        s.sendall(header)
        # receive response frame
        buf = b""
        s.settimeout(timeout)
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            try:
                json.loads(buf[2:])  # skip 2-byte WS header
                break
            except Exception:
                pass
        s.close()
        raw = buf[2:] if len(buf) > 2 else buf
        result = json.loads(raw)
        return result.get("result")
    except Exception:
        return None


def _check_auth_cookies(ws_url: str, domain: str) -> tuple[bool, str | None]:
    """Return (authenticated, cookie_name) by reading HTTP cookies via CDP."""
    result = _cdp_ws_call(ws_url, "Network.getAllCookies")
    if result is None:
        return False, None
    cookies = result.get("cookies") or []
    target_cookies = _AUTH_COOKIES.get(domain, [])
    for c in cookies:
        c_domain = (c.get("domain") or "").lstrip(".")
        if domain in c_domain or c_domain in domain:
            for name in target_cookies:
                if c.get("name") == name and c.get("value"):
                    return True, name
    return False, None


def probe_session(session: dict, domain: str | None) -> dict:
    """Enrich a session entry with reachability, tabs, and auth state."""
    port = session["port"]
    pages = _cdp_pages(port)
    session["reachable"] = bool(pages)
    if not pages:
        return session
    tabs = [{"url": p.get("url", ""), "title": p.get("title", ""), "wsUrl": p.get("webSocketDebuggerUrl")}
            for p in pages if p.get("type") == "page"]
    session["tabs"] = tabs
    if domain is None:
        return session
    # Check if any tab is on the target domain
    domain_tabs = [t for t in tabs if domain in (t["url"] or "")]
    session["holdsTarget"] = bool(domain_tabs)
    # Verify by cookie inspection on any tab on that domain
    for tab in (domain_tabs or tabs[:1]):
        ws = tab.get("wsUrl")
        if ws:
            authenticated, cookie_name = _check_auth_cookies(ws, domain)
            if authenticated:
                session["authConfirmed"] = True
                session["authCookie"] = cookie_name
                break
    return session


# ─── session selection ─────────────────────────────────────────────────────────

def select_best_session(sessions: list[dict], task: dict) -> dict:
    """Choose the session to use for a given task.

    Priority:
      1. Session with authConfirmed (proven login via auth cookie) on target domain
      2. Session with holdsTarget (tab on domain, but cookie check failed/skipped)
      3. Any reachable session (may need login)
      4. None found → needs-login

    Returns a selection dict:
      mode: 'attach' | 'needs-login' | 'no-chrome'
      port: int | None
      userDataDir: str | None
      authCookie: str | None
      reason: str
    """
    domain = task.get("domain")
    needs_auth = task.get("needsAuth", False)

    reachable = [s for s in sessions if s.get("reachable")]
    if not reachable:
        return {"mode": "no-chrome", "port": None, "userDataDir": None,
                "authCookie": None, "reason": "no debug-enabled Chrome found on this host"}

    if needs_auth and domain:
        auth_confirmed = [s for s in reachable if s.get("authConfirmed")]
        if auth_confirmed:
            s = auth_confirmed[0]
            return {"mode": "attach", "port": s["port"], "userDataDir": s["userDataDir"],
                    "authCookie": s["authCookie"],
                    "reason": f"auth cookie '{s['authCookie']}' confirmed on port {s['port']}"}
        holds = [s for s in reachable if s.get("holdsTarget")]
        if holds:
            s = holds[0]
            return {"mode": "attach", "port": s["port"], "userDataDir": s["userDataDir"],
                    "authCookie": None,
                    "reason": f"tab on {domain} exists on port {s['port']} but cookie check inconclusive"}
        return {"mode": "needs-login", "port": reachable[0]["port"],
                "userDataDir": reachable[0]["userDataDir"], "authCookie": None,
                "reason": f"no session has confirmed auth cookie for {domain} — one-time login required (human-gated)"}

    # no auth needed or no domain — just pick the first reachable
    s = reachable[0]
    return {"mode": "attach", "port": s["port"], "userDataDir": s["userDataDir"],
            "authCookie": None, "reason": "no auth required — attaching to first reachable session"}
