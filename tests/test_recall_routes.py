"""Tests for twin://host/flow/query/recall and twin://host/flow/episode/command/run.

Both handlers are tested without a live mesh — episode_store is populated directly
via durable_memory() isolated in a temp file per test."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from urirun_connector_twin import core


def _ok_episode(episode_id: str, goal: str, steps: list, env_fp: str = "fp-abc") -> dict:
    from urirun.node.episode import intent_signature
    return {
        "episode_id": episode_id,
        "goal": goal,
        "intent_sig": intent_signature(goal),
        "plan": {"steps": steps, "flow_key": "fk-test"},
        "reality": {"fingerprint": env_fp},
        "outcome": {"status": "ok"},
        "ts": "2026-01-01T00:00:00Z",
    }


class TestFlowRecallRoute(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recall-routes-")
        self.path = os.path.join(self.tmp, "twin-memory.json")
        self._old = os.environ.get("URIRUN_TWIN_MEMORY")
        os.environ["URIRUN_TWIN_MEMORY"] = self.path

    def tearDown(self):
        if self._old is None:
            os.environ.pop("URIRUN_TWIN_MEMORY", None)
        else:
            os.environ["URIRUN_TWIN_MEMORY"] = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mem(self):
        from urirun.node.twin_store import durable_memory
        return durable_memory()

    def test_recall_by_episode_id_direct(self):
        steps = [{"id": "s", "uri": "kvm://host/screen/query/capture"}]
        ep = _ok_episode("ep-abc123", "zrób screenshot", steps)
        self._mem().remember_episode(ep)
        result = core.flow_recall(episode_id="ep-abc123")
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("found"))
        self.assertEqual(result.get("source"), "episode")
        self.assertEqual(len(result.get("steps") or []), 1)

    def test_recall_by_intent_and_env_fp(self):
        steps = [{"id": "s", "uri": "kvm://host/screen/query/capture"}]
        ep = _ok_episode("ep-bbb", "capture screen", steps, env_fp="fp-xyz")
        self._mem().remember_episode(ep)
        result = core.flow_recall(prompt="capture screen", env_fp="fp-xyz")
        self.assertTrue(result.get("found"))
        self.assertEqual(result.get("source"), "episode")

    def test_recall_intent_only_fallback_via_flow_store(self):
        """When env_fp doesn't match episode, falls back to flow_store by intent."""
        from urirun.node.episode import intent_signature
        steps = [{"id": "s", "uri": "kvm://host/screen/query/capture"}]
        mem = self._mem()
        mem.remember_flow("fk-x", {
            "flowKey": "fk-x",
            "intent_sig": intent_signature("daily report"),
            "steps": steps,
            "degraded": False,
            "ts": "2026-01-01T00:00:00Z",
        })
        result = core.flow_recall(prompt="daily report", env_fp="wrong-fp")
        self.assertTrue(result.get("found"))
        self.assertEqual(result.get("source"), "flow_store")
        self.assertEqual(len(result.get("steps") or []), 1)

    def test_recall_returns_not_found_for_unknown_prompt(self):
        result = core.flow_recall(prompt="something never seen before")
        self.assertTrue(result.get("ok"))
        self.assertFalse(result.get("found"))
        self.assertEqual(result.get("steps"), [])

    def test_recall_episode_id_missing_returns_not_found(self):
        result = core.flow_recall(episode_id="ep-doesnotexist")
        self.assertFalse(result.get("found"))


class TestFlowEpisodeRun(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-run-")
        self.path = os.path.join(self.tmp, "twin-memory.json")
        self._old = os.environ.get("URIRUN_TWIN_MEMORY")
        os.environ["URIRUN_TWIN_MEMORY"] = self.path

    def tearDown(self):
        if self._old is None:
            os.environ.pop("URIRUN_TWIN_MEMORY", None)
        else:
            os.environ["URIRUN_TWIN_MEMORY"] = self._old
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_episode_run_missing_episode_returns_error(self):
        result = core.flow_episode_run(episode_id="ep-ghost")
        self.assertFalse(result.get("ok"))
        self.assertFalse(result.get("found"))
        err = result.get("error") or {}
        self.assertEqual(err.get("category"), "NOT_FOUND")

    def test_episode_run_dispatches_stored_steps(self):
        steps = [{"id": "s", "uri": "twin://host/env/query/drift", "payload": {}, "depends_on": []}]
        ep = _ok_episode("ep-run1", "check drift", steps)
        from urirun.node.twin_store import durable_memory
        durable_memory().remember_episode(ep)
        dispatched: list[str] = []
        def fake_call(uri, payload=None, *a, **kw):
            dispatched.append(uri)
            return {"ok": True, "result": {"value": {"ok": True, "drift": False}}}
        with mock.patch("urirun.v2_service.call", side_effect=fake_call):
            result = core.flow_episode_run(episode_id="ep-run1", execute=True)
        self.assertTrue(result.get("ok"), result)
        self.assertIn("twin://host/env/query/drift", dispatched)


if __name__ == "__main__":
    unittest.main()
