"""Prompt → imperative plan: derives concrete URI steps from a natural language prompt
without a pre-built flow.  No LLM required — uses keyword + intent pattern matching
to produce a best-effort ordered step list that the twin planner can then annotate
with feasibility and reversibility.

For each recognized intent category, a step template is expanded with values
extracted from the prompt (URL, text, query terms, etc.).  Unknown/unrecognized
prompts get a single `env://host/intent/query/describe` fallback step.

This is the "gather grounding data from the prompt" half of the twin loop.
The other half (`annotate_steps`) handles feasibility against the live environment."""
from __future__ import annotations

import re
from typing import Any


# ─── intent patterns ─────────────────────────────────────────────────────────

def _extract_url(text: str) -> str | None:
    m = re.search(r"https?://[^\s\"'>]+", text)
    return m.group(0) if m else None


def _extract_domain(text: str) -> str | None:
    url = _extract_url(text)
    if url:
        m = re.match(r"https?://([^/]+)", url)
        return m.group(1) if m else None
    # e.g. "post on linkedin" → linkedin.com
    for svc in ("linkedin", "github", "google", "facebook", "twitter", "youtube"):
        if svc in text.lower():
            return f"{svc}.com"
    return None


def _extract_text_to_type(text: str) -> str:
    m = re.search(r'"([^"]+)"', text)
    if m:
        return m.group(1)
    for kw in ("write", "type", "enter", "fill", "wpisz", "napisz"):
        idx = text.lower().find(kw)
        if idx >= 0:
            after = text[idx + len(kw):].strip()
            if after:
                return after.split(".")[0].strip()[:200]
    return ""


def _extract_query(text: str) -> str:
    for kw in ("search for", "find", "szukaj", "znajdź"):
        idx = text.lower().find(kw)
        if idx >= 0:
            return text[idx + len(kw):].strip()[:200]
    return text.strip()[:100]


# ─── step templates ──────────────────────────────────────────────────────────

def _browser_open_steps(url: str) -> list[dict]:
    return [
        {"id": "session_ensure", "uri": "kvm://{node}/cdp/session/command/ensure",
         "payload": {"url": url}},
        {"id": "navigate", "uri": "kvm://{node}/cdp/page/command/navigate",
         "payload": {"url": url}},
        {"id": "page_ready", "uri": "kvm://{node}/cdp/session/query/ready",
         "payload": {}},
    ]


def _browser_fill_and_submit_steps(url: str, field_text: str) -> list[dict]:
    steps = _browser_open_steps(url)
    steps += [
        {"id": "fill_field", "uri": "kvm://{node}/cdp/page/command/fill",
         "payload": {"role": "textbox", "text": field_text}},
        {"id": "submit", "uri": "kvm://{node}/cdp/page/command/click",
         "payload": {"role": "button", "text": "Submit"}},
    ]
    return steps


def _post_on_social_steps(domain: str, content: str) -> list[dict]:
    url = f"https://{domain}"
    return [
        {"id": "session_ensure", "uri": "kvm://{node}/cdp/session/command/ensure",
         "payload": {"url": url}},
        {"id": "navigate_home", "uri": "kvm://{node}/cdp/page/command/navigate",
         "payload": {"url": url}},
        {"id": "page_ready", "uri": "kvm://{node}/cdp/session/query/ready",
         "payload": {}},
        {"id": "click_compose", "uri": "kvm://{node}/cdp/page/command/click",
         "payload": {"role": "button", "text": "Start a post"}},
        {"id": "fill_post", "uri": "kvm://{node}/cdp/page/command/fill",
         "payload": {"role": "textbox", "text": content or "New post"}},
        {"id": "click_publish", "uri": "kvm://{node}/cdp/page/command/click",
         "payload": {"role": "button", "text": "Post"}},
    ]


def _search_steps(query: str) -> list[dict]:
    url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
    return [
        {"id": "session_ensure", "uri": "kvm://{node}/cdp/session/command/ensure",
         "payload": {"url": url}},
        {"id": "navigate_search", "uri": "kvm://{node}/cdp/page/command/navigate",
         "payload": {"url": url}},
        {"id": "page_ready", "uri": "kvm://{node}/cdp/session/query/ready",
         "payload": {}},
    ]


def _screenshot_steps() -> list[dict]:
    return [
        {"id": "capture_screen", "uri": "kvm://{node}/display/query/screenshot",
         "payload": {}},
    ]


def _file_write_steps(path: str, content: str) -> list[dict]:
    return [
        {"id": "write_file", "uri": "kvm://{node}/file/command/write",
         "payload": {"path": path, "content": content}},
    ]


def _service_start_steps(service: str) -> list[dict]:
    return [
        {"id": f"start_{service}", "uri": f"dashboard://host/service/{service}/start",
         "payload": {}},
    ]


def _service_stop_steps(service: str) -> list[dict]:
    return [
        {"id": f"stop_{service}", "uri": f"dashboard://host/service/{service}/stop",
         "payload": {}},
    ]


def _fallback_describe_steps(prompt: str) -> list[dict]:
    return [
        {"id": "describe_intent", "uri": "env://{node}/intent/query/describe",
         "payload": {"prompt": prompt}},
    ]


# ─── intent detection ─────────────────────────────────────────────────────────

_SOCIAL_VERBS = ("post", "publish", "share", "opublikuj", "udostępnij")
_SEARCH_VERBS = ("search", "find", "look up", "szukaj", "znajdź")
_FILL_VERBS = ("fill", "type", "enter", "write", "wpisz", "napisz")
_SCREEN_VERBS = ("screenshot", "capture screen", "zrzut ekranu")
_OPEN_VERBS = ("open", "navigate", "go to", "otwórz", "przejdź")
_SERVICE_START = ("start", "uruchom", "włącz")
_SERVICE_STOP = ("stop", "restart", "zatrzymaj", "wyłącz")


def derive_task_target(prompt: str) -> dict:
    """Extract domain, content, and task type from a natural language prompt.

    Returns: {domain, url, content, needsAuth, taskType}"""
    low = prompt.lower()
    domain = _extract_domain(prompt)
    url = _extract_url(prompt)
    content = _extract_text_to_type(prompt)
    needs_auth = any(svc in low for svc in ("linkedin", "github", "facebook", "instagram", "twitter"))
    task_type = "unknown"

    if any(v in low for v in _SOCIAL_VERBS) and domain:
        task_type = "social-post"
    elif any(v in low for v in _SEARCH_VERBS):
        task_type = "web-search"
    elif any(v in low for v in _FILL_VERBS) and (url or domain):
        task_type = "browser-fill"
    elif any(v in low for v in _OPEN_VERBS) and (url or domain):
        task_type = "browser-open"
    elif any(v in low for v in _SCREEN_VERBS):
        task_type = "screenshot"
    elif any(v in low for v in _SERVICE_START):
        task_type = "service-start"
    elif any(v in low for v in _SERVICE_STOP):
        task_type = "service-stop"
    elif url:
        task_type = "browser-open"

    return {
        "domain": domain,
        "url": url,
        "content": content,
        "needsAuth": needs_auth,
        "taskType": task_type,
    }


def steps_from_prompt(prompt: str, node: str = "host") -> list[dict]:
    """Derive concrete URI step list from a natural language prompt.

    Steps use `{node}` as a placeholder — caller substitutes the actual node name."""
    low = prompt.lower()
    target = derive_task_target(prompt)
    task_type = target["taskType"]
    domain = target["domain"]
    url = target["url"]
    content = target["content"]

    raw: list[dict] = []

    if task_type == "social-post" and domain:
        raw = _post_on_social_steps(domain, content)
    elif task_type == "web-search":
        raw = _search_steps(_extract_query(prompt))
    elif task_type == "browser-fill" and (url or domain):
        raw = _browser_fill_and_submit_steps(url or f"https://{domain}", content)
    elif task_type == "browser-open" and (url or domain):
        raw = _browser_open_steps(url or f"https://{domain}")
    elif task_type == "screenshot":
        raw = _screenshot_steps()
    elif task_type == "service-start":
        svc = _guess_service_name(prompt)
        raw = _service_start_steps(svc)
    elif task_type == "service-stop":
        svc = _guess_service_name(prompt)
        raw = _service_stop_steps(svc)
    else:
        raw = _fallback_describe_steps(prompt)

    # Substitute {node} placeholder
    return [_bind_node(s, node) for s in raw]


def _guess_service_name(prompt: str) -> str:
    m = re.search(r"(?:start|stop|restart|uruchom|zatrzymaj)\s+(\w[\w-]*)", prompt, re.I)
    return m.group(1).lower() if m else "service"


def _bind_node(step: dict, node: str) -> dict:
    return {**step, "uri": step["uri"].replace("{node}", node)}


# ─── top-level plan builder ───────────────────────────────────────────────────

def plan_from_prompt(prompt: str, node: str = "host") -> dict:
    """Build a raw (unannotated) imperative plan dict from a prompt.

    This is the input to `planner.build_imperative_plan` — it produces the
    `flow`-shaped dict with steps, plus derived task metadata."""
    target = derive_task_target(prompt)
    steps = steps_from_prompt(prompt, node)
    return {
        "prompt": prompt,
        "taskType": target["taskType"],
        "domain": target["domain"],
        "needsAuth": target["needsAuth"],
        "steps": steps,
        "stepCount": len(steps),
    }
