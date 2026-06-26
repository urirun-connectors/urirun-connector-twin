"""Browser session discovery + selection tests.

These test browser.py (the canonical implementation) and the URI dispatch layer.
session.py has been removed — its logic is now in browser.py + prompt_plan.py."""
import pytest

from urirun_connector_twin.browser import (
    discover_browser_sessions,
    select_session,
    _cdp_pages,
    _cdp_cookies,
    _has_auth_cookie,
    _domain_key,
    _extract_flag,
    _is_browser,
)
from urirun_connector_twin.prompt_plan import derive_task_target
from urirun_connector_twin.dispatch import uri_call, value_of


# ─── derive_task_target (prompt_plan) ────────────────────────────────────────

def test_derive_linkedin():
    t = derive_task_target("opublikuj post na linkedin")
    assert t["domain"] == "linkedin.com"
    assert t["needsAuth"] is True


def test_derive_github():
    t = derive_task_target("open a PR on github")
    assert t["domain"] == "github.com"
    assert t["needsAuth"] is True


def test_derive_google_no_auth():
    t = derive_task_target("search on google")
    assert t["domain"] == "google.com"
    assert t["needsAuth"] is False


def test_derive_unknown_prompt():
    t = derive_task_target("list files in /tmp")
    assert t["domain"] is None
    assert t["needsAuth"] is False


def test_derive_empty_prompt():
    t = derive_task_target("")
    assert t["domain"] is None


# ─── _extract_flag + _is_browser (browser.py primitives) ─────────────────────

def test_extract_flag_debug_port():
    args = [b"google-chrome", b"--remote-debugging-port=9222", b"--no-first-run"]
    assert _extract_flag(args, "remote-debugging-port") == "9222"


def test_extract_flag_user_data_dir():
    args = [b"chromium", b"--user-data-dir=/home/tom/.config/google-chrome"]
    assert _extract_flag(args, "user-data-dir") == "/home/tom/.config/google-chrome"


def test_extract_flag_missing_returns_none():
    assert _extract_flag([b"chrome"], "remote-debugging-port") is None


def test_is_browser_chrome():
    assert _is_browser([b"/usr/bin/google-chrome"]) is True


def test_is_browser_chromium():
    assert _is_browser([b"chromium-browser"]) is True


def test_is_browser_not_chrome():
    assert _is_browser([b"firefox"]) is False


def test_is_browser_empty():
    assert _is_browser([]) is False


# ─── _domain_key ──────────────────────────────────────────────────────────────

def test_domain_key_linkedin():
    assert _domain_key("linkedin.com") == "linkedin"


def test_domain_key_github():
    assert _domain_key("github.com") == "github"


def test_domain_key_unknown():
    # unknown domain → first segment
    assert _domain_key("example.com") == "example"


# ─── _has_auth_cookie ────────────────────────────────────────────────────────

def test_has_auth_cookie_linkedin_found():
    cookies = [{"name": "li_at", "domain": ".linkedin.com", "value": "AQED"}]
    assert _has_auth_cookie(cookies, "linkedin") is True


def test_has_auth_cookie_linkedin_missing():
    cookies = [{"name": "other_cookie", "domain": ".linkedin.com", "value": "x"}]
    assert _has_auth_cookie(cookies, "linkedin") is False


def test_has_auth_cookie_unknown_domain_any_match():
    cookies = [{"name": "session", "domain": "example.com", "value": "abc"}]
    assert _has_auth_cookie(cookies, "example") is True


# ─── select_session ───────────────────────────────────────────────────────────

def _make_session(port, udd, reachable=True, pages=None, auth=None):
    s = {"pid": 1, "port": port, "userDataDir": udd, "reachable": reachable,
         "pages": pages or []}
    if auth is not None:
        s["authCookies"] = auth
    return s


def test_select_prefers_tab_on_domain_with_auth():
    sessions = [
        _make_session(9333, "/home/tom/.config/google-chrome",
                      pages=[{"url": "https://linkedin.com/feed", "title": ""}],
                      auth={"linkedin": True}),
        _make_session(9222, "/tmp/urirun-kvm-cdp-9222", pages=[], auth={"linkedin": False}),
    ]
    sel = select_session(sessions, "linkedin.com", needs_auth=True)
    assert sel["mode"] == "attach"
    assert sel["port"] == 9333
    assert sel["profileType"] == "real"


def test_select_real_profile_over_tmp():
    sessions = [
        _make_session(9222, "/tmp/urirun-kvm-cdp-9222", pages=[]),
        _make_session(9333, "/home/tom/.config/google-chrome", pages=[]),
    ]
    sel = select_session(sessions, "linkedin.com", needs_auth=False)
    assert sel["mode"] == "attach"
    assert sel["port"] == 9333


def test_select_needs_login_when_no_reachable():
    sel = select_session([], "linkedin.com", needs_auth=True)
    assert sel["mode"] == "needs-login"
    assert sel["humanGated"] is True


def test_select_no_needs_auth_picks_first_reachable():
    sessions = [_make_session(9222, "/tmp/x", pages=[])]
    sel = select_session(sessions, "google.com", needs_auth=False)
    assert sel["mode"] == "attach"


# ─── discover_browser_sessions (monkeypatched) ───────────────────────────────

def test_discover_returns_list(monkeypatch):
    monkeypatch.setattr("urirun_connector_twin.browser.os.listdir", lambda p: [])
    sessions = discover_browser_sessions()
    assert isinstance(sessions, list)


# ─── dispatch.uri_call ───────────────────────────────────────────────────────

def test_uri_call_returns_none_on_no_mesh():
    # v2_service not available in test env → fallback invoked
    result = uri_call("twin://host/not-a-real-route", {}, fallback=lambda: {"ok": True, "x": 1})
    assert result == {"ok": True, "x": 1}


def test_uri_call_fallback_none_returns_none():
    result = uri_call("twin://host/not-a-real-route", {})
    assert result is None


def test_value_of_extracts_nested():
    r = {"ok": True, "value": {"a": 1}}
    assert value_of(r) == {"a": 1}


def test_value_of_none_input():
    assert value_of(None) is None


# ─── constraints route (via connector) ───────────────────────────────────────

def test_constraints_from_profile_route_empty_matrix():
    from urirun_connector_twin.core import constraints_from_profile
    r = constraints_from_profile(actionMatrix={})
    assert r["ok"] is True
    assert isinstance(r["constraints"], list)


def test_constraints_from_profile_route_blocked_surface():
    from urirun_connector_twin.core import constraints_from_profile
    r = constraints_from_profile(actionMatrix={
        "atspi": {"type": "not_executable"},
        "uinput": {"type": "not_executable"},
        "cdp": {"type": "executable"},
    })
    assert r["ok"] is True
    # May return constraints if reversible module available
    assert isinstance(r["constraints"], list)


# ─── environment integration ──────────────────────────────────────────────────

def test_probe_adds_infeasible_when_needs_login(monkeypatch):
    monkeypatch.setattr("urirun_connector_twin.environment._kvm_query", lambda *a: None)
    monkeypatch.setattr("urirun_connector_twin.environment._docker_available", lambda: False)
    monkeypatch.setattr("urirun_connector_twin.environment.uri_call", lambda *a, **kw: None)
    monkeypatch.setattr("urirun_connector_twin.browser.os.listdir",
                        lambda p: [])  # no chrome processes

    from urirun_connector_twin.environment import probe
    env = probe(None, prompt="opublikuj post na linkedin")
    sel = env["sessionSelection"]
    assert sel["mode"] in ("needs-login", "no-target")


def test_browser_profile_handler_no_chrome(monkeypatch):
    monkeypatch.setattr("urirun_connector_twin.browser.os.listdir", lambda p: [])
    from urirun_connector_twin.core import browser_profile
    r = browser_profile(domain="linkedin.com", probe_cookies=False)
    assert r["ok"] is True
    assert r["selection"]["mode"] == "needs-login"
