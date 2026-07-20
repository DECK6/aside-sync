from __future__ import annotations

import json
import runpy
import tempfile
import unittest
from pathlib import Path

from tests.common import SYNCD, create_fixture, daemon_config, run_cli


SESSION = "sessionABC123456"


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-tests-")
        self.base = Path(self.tmp.name)
        self.api = runpy.run_path(str(SYNCD))
        self.sync_dir = self.base / "shared"
        (self.sync_dir / "locks").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_acquire_renew_foreign_expiry_takeover_and_release_tombstone(self):
        acquire = self.api["try_acquire_lock"]
        release = self.api["release_lock"]
        ok, first = acquire(self.sync_dir, SESSION, "A", "h1", 100, 10, now=1000)
        self.assertTrue(ok)
        self.assertEqual(first["acquiredAt"], 1000)
        ok, renewed = acquire(self.sync_dir, SESSION, "A", "h2", 100, 10, now=1050)
        self.assertTrue(ok)
        self.assertEqual(renewed["acquiredAt"], 1000)
        self.assertEqual(renewed["leaseUntil"], 1150)
        ok, held = acquire(self.sync_dir, SESSION, "B", "hb", 100, 10, now=1100)
        self.assertFalse(ok)
        self.assertEqual(held["ownerDeviceId"], "A")
        ok, expired = acquire(self.sync_dir, SESSION, "B", "hb", 100, 10, now=1161)
        self.assertTrue(ok)
        self.assertEqual(expired["ownerDeviceId"], "B")
        ok, takeover = acquire(self.sync_dir, SESSION, "A", "ha", 100, 10, now=1162, force=True)
        self.assertTrue(ok)
        self.assertTrue(takeover["takeover"])
        tombstone = release(self.sync_dir, SESSION, "A", now=1170)
        lock_path = self.sync_dir / "locks" / f"{SESSION}.json"
        self.assertTrue(lock_path.exists())
        self.assertEqual(tombstone["leaseUntil"], 0)
        self.assertEqual(json.loads(lock_path.read_text())["leaseUntil"], 0)

    def test_run_once_skips_export_under_valid_foreign_lease(self):
        root = create_fixture(self.base / "root", SESSION)
        cfg = self.base / "config.json"
        state = self.base / "state.json"
        daemon_config(cfg, root, self.sync_dir, "A")
        self.api["try_acquire_lock"](self.sync_dir, SESSION, "B", "foreign", 900, 120, now=self.api["now_epoch"]())
        result = run_cli(SYNCD, "--config", cfg, "--state", state, "run-once")
        report = json.loads(result.stdout)
        self.assertEqual(report["exported"], [])
        self.assertTrue(any(item["reason"] == "foreign-lock" for item in report["skipped"]))
        self.assertFalse((self.sync_dir / "bundles" / SESSION).exists())


if __name__ == "__main__":
    unittest.main()
