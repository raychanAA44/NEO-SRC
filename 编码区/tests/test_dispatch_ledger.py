#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_dispatch_ledger.py - 02 派发台账（NEO-UI-02）单测。"""

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from core.dispatch_ledger import DispatchLedger


class _Clock:
    def __init__(self, start=1000):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestDispatchLedger(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self.ledger = DispatchLedger(maxlen=3, clock=self.clock)

    def _start(self, rid="rid-1", task_id="task-1", model="trae/m1", group="auto/g",
               locked=False, tier="strong", priority=1, lock_state="BOUND", ttl=100):
        return self.ledger.start(
            rid=rid, task_id=task_id, requested_model=group, model=model,
            group_id=group, locked=locked, tier=tier, priority=priority,
            policy={"task_lock": True, "smart_tier": True},
            lock_state=lock_state, lock_ttl=ttl, dispatch_ms=0.1836,
            path="/v1/chat/completions")

    def test_start_records_gateway_facts(self):
        trace = self._start()
        self.assertTrue(trace["trace_id"])
        self.assertEqual(trace["rid"], "rid-1")
        self.assertEqual(trace["group_id"], "auto/g")
        self.assertEqual(trace["path"], "/v1/chat/completions")
        self.assertEqual(trace["model"], "trae/m1")
        self.assertEqual(trace["tier"], "strong")
        self.assertEqual(trace["priority"], 1)
        self.assertEqual(trace["lock_state"], "BOUND")
        self.assertEqual(trace["lock_ttl"], 100)
        self.assertEqual(trace["dispatch_ms"], 0.184, "派发耗时四舍五入到 ms 千分位")
        self.assertEqual(trace["state"], "running")
        self.assertIsNone(trace["upstream_status"])
        self.assertEqual(trace["at"], 1000)

    def test_finish_backfills_upstream_and_error(self):
        trace = self._start()
        self.clock.advance(2)
        rec = self.ledger.finish(trace["trace_id"], status=400, ms=812.3456,
                                 response_bytes=42, error="bad request")
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["upstream_status"], 400)
        self.assertEqual(rec["upstream_ms"], 812.346)
        self.assertEqual(rec["response_bytes"], 42)
        self.assertEqual(rec["error"], "bad request")
        self.assertEqual(rec["finished_at"], 1002)
        self.assertEqual(self.ledger.get(trace["trace_id"])["upstream_status"], 400)

    def test_finish_unknown_trace_is_noop(self):
        self.assertIsNone(self.ledger.finish("missing", status=200, ms=1))

    def test_bind_rid_keeps_local_request_id(self):
        trace = self._start(rid="local-req-1")
        updated = self.ledger.bind_rid(trace["trace_id"], "upstream-rid-9")
        self.assertEqual(updated["rid"], "upstream-rid-9")
        self.assertEqual(updated["request_id"], "local-req-1")
        self.assertEqual(updated["upstream_rid"], "upstream-rid-9")
        self.assertEqual(self.ledger.get(trace["trace_id"])["rid"], "upstream-rid-9")

    def test_bind_empty_rid_is_noop(self):
        trace = self._start(rid="local-req-1")
        updated = self.ledger.bind_rid(trace["trace_id"], "  ")
        self.assertEqual(updated["rid"], "local-req-1")
        self.assertEqual(updated["upstream_rid"], "")
        self.assertIsNone(self.ledger.bind_rid("missing", "upstream-rid-9"))

    def test_snapshot_newest_first_and_limit(self):
        first = self._start(rid="r1", model="trae/m1")
        self.clock.advance(1)
        second = self._start(rid="r2", model="trae/m2")
        self.clock.advance(1)
        third = self._start(rid="r3", model="trae/m3")
        rows = self.ledger.snapshot(2)
        self.assertEqual([r["trace_id"] for r in rows],
                         [third["trace_id"], second["trace_id"]])
        self.assertEqual(self.ledger.snapshot(50)[0]["trace_id"], third["trace_id"])
        self.assertEqual(rows[0]["rid"], "r3")
        self.assertEqual(self.ledger.count(), 3)
        self.assertNotEqual(first["trace_id"], third["trace_id"])

    def test_ring_evicts_oldest_and_drops_it_from_lookup(self):
        first = self._start(rid="r1")
        self._start(rid="r2")
        self._start(rid="r3")
        self.clock.advance(1)
        fourth = self._start(rid="r4")
        self.assertEqual(self.ledger.count(), 3)
        self.assertIsNone(self.ledger.get(first["trace_id"]))
        self.assertIsNotNone(self.ledger.get(fourth["trace_id"]))
        self.assertEqual([r["rid"] for r in self.ledger.snapshot(10)],
                         ["r4", "r3", "r2"])

    def test_snapshot_clamps_invalid_limits(self):
        self._start()
        self.assertEqual(len(self.ledger.snapshot("bad")), 1)
        self.assertEqual(len(self.ledger.snapshot(0)), 1)
        self.assertEqual(len(self.ledger.snapshot(9999)), 1)

    def test_policy_is_copied_not_shared(self):
        policy = {"task_lock": True}
        trace = self.ledger.start(
            rid="r", task_id="t", requested_model="auto/g", model="trae/m",
            group_id="auto/g", locked=False, policy=policy)
        policy["task_lock"] = False
        self.assertTrue(trace["policy"]["task_lock"])
        self.assertTrue(self.ledger.get(trace["trace_id"])["policy"]["task_lock"])

    def test_clear_empties_all(self):
        self._start()
        self.ledger.clear()
        self.assertEqual(self.ledger.count(), 0)
        self.assertEqual(self.ledger.snapshot(10), [])

    def test_persist_and_reload_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dispatch.db"
            ledger = DispatchLedger(maxlen=3, clock=self.clock, persist_path=db)
            trace = ledger.start(
                rid="local-1", task_id="task-1", requested_model="auto/g",
                model="trae/m1", group_id="auto/g", locked=True, tier="strong",
                priority=1, policy={"task_lock": True}, lock_state="HIT",
                lock_ttl=42, dispatch_ms=0.2, path="/v1/chat/completions")
            ledger.bind_rid(trace["trace_id"], "upstream-9")
            self.clock.advance(2)
            ledger.finish(trace["trace_id"], status=200, ms=812.5,
                          response_bytes=99)

            reborn = DispatchLedger(maxlen=3, clock=self.clock, persist_path=db)
            self.assertEqual(reborn.count(), 1)
            rec = reborn.get(trace["trace_id"])
            self.assertEqual(rec["rid"], "upstream-9")
            self.assertEqual(rec["request_id"], "local-1")
            self.assertEqual(rec["locked"], True)
            self.assertEqual(rec["tier"], "strong")
            self.assertEqual(rec["policy"], {"task_lock": True})
            self.assertEqual(rec["upstream_status"], 200)
            self.assertEqual(rec["response_bytes"], 99)
            self.assertEqual(rec["finished_at"], 1002)

            reborn.clear(persisted=True)
            self.assertEqual(DispatchLedger(maxlen=3, clock=self.clock,
                                            persist_path=db).count(), 0)

    def test_persist_ring_prunes_old_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "dispatch.db"
            ledger = DispatchLedger(maxlen=2, clock=self.clock, persist_path=db)
            first = ledger.start(
                rid="r1", task_id="t", requested_model="auto/g", model="m1",
                group_id="auto/g", locked=False)
            self.clock.advance(1)
            ledger.start(rid="r2", task_id="t", requested_model="auto/g",
                         model="m2", group_id="auto/g", locked=False)
            self.clock.advance(1)
            ledger.start(rid="r3", task_id="t", requested_model="auto/g",
                         model="m3", group_id="auto/g", locked=False)
            reborn = DispatchLedger(maxlen=2, clock=self.clock, persist_path=db)
            self.assertEqual(reborn.count(), 2)
            self.assertIsNone(reborn.get(first["trace_id"]))
            self.assertEqual([r["rid"] for r in reborn.snapshot(10)],
                             ["r3", "r2"])
            ledger.clear(persisted=True)


if __name__ == "__main__":
    unittest.main()
