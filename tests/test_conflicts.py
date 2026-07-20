from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.common import SYNCD, append_message, create_fixture, daemon_config, empty_target, run_cli


SID = "sessionABC123456"


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-tests-")
        self.base = Path(self.tmp.name)
        self.root_a = create_fixture(self.base / "a", SID)
        self.root_b = empty_target(self.base / "b")
        self.shared = self.base / "shared"
        self.config_a = self.base / "a.json"
        self.config_b = self.base / "b.json"
        self.state_a = self.base / "a-state.json"
        self.state_b = self.base / "b-state.json"
        daemon_config(self.config_a, self.root_a, self.shared, "device-a")
        daemon_config(self.config_b, self.root_b, self.shared, "device-b")
        self.cycle(self.config_a, self.state_a)
        self.cycle(self.config_b, self.state_b)

    def tearDown(self):
        self.tmp.cleanup()

    def cycle(self, config, state):
        return json.loads(run_cli(SYNCD, "--config", config, "--state", state, "run-once").stdout)

    def diverge_and_record(self):
        local_hash = append_message(self.root_b, SID, "local-B")
        remote_hash = append_message(self.root_a, SID, "remote-A")
        self.cycle(self.config_a, self.state_a)
        report = self.cycle(self.config_b, self.state_b)
        records = sorted((self.shared / "conflicts").glob(f"{SID}-*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text())
        self.assertEqual(
            set(record),
            {"sessionId", "localDeviceId", "sourceDeviceId", "localHash", "remoteHash", "bundlePath", "detectedAt", "resolution"},
        )
        self.assertEqual(record["localHash"], local_hash)
        self.assertEqual(record["remoteHash"], remote_hash)
        self.assertIsNone(record["resolution"])
        self.assertEqual(len(report["conflicts"]), 1)
        return records[0], record

    def test_divergence_records_exact_conflict_without_overwrite(self):
        before = append_message(self.root_b, SID, "local-B")
        append_message(self.root_a, SID, "remote-A")
        self.cycle(self.config_a, self.state_a)
        self.cycle(self.config_b, self.state_b)
        sdir = next((self.root_b / "agents/main/sessions").glob(f"*_{SID}"))
        self.assertEqual(__import__("hashlib").sha256((sdir / "messages.jsonl").read_bytes()).hexdigest(), before)
        record = json.loads(next((self.shared / "conflicts").glob(f"{SID}-*.json")).read_text())
        self.assertIsNone(record["resolution"])

    def test_fork_policy_imports_remote_as_new_session(self):
        config = json.loads(self.config_b.read_text())
        config["conflictPolicy"] = "fork"
        self.config_b.write_text(json.dumps(config), encoding="utf-8")
        append_message(self.root_b, SID, "local-B")
        append_message(self.root_a, SID, "remote-A")
        self.cycle(self.config_a, self.state_a)
        self.cycle(self.config_b, self.state_b)
        record = json.loads(next((self.shared / "conflicts").glob(f"{SID}-*.json")).read_text())
        self.assertTrue(record["resolution"].startswith("forked:"))
        new_sid = record["resolution"].split(":", 1)[1]
        con = sqlite3.connect(self.root_b / "state.db")
        row = con.execute("SELECT branched_from,title FROM sessions WHERE id=?", (new_sid,)).fetchone()
        con.close()
        self.assertEqual(row[0], SID)
        self.assertIn("conflict from device-a", row[1])

    def test_resolve_take_remote(self):
        path, record = self.diverge_and_record()
        result = json.loads(run_cli(
            SYNCD, "--config", self.config_b, "--state", self.state_b,
            "resolve", SID, "--take", "remote",
        ).stdout)
        self.assertEqual(result["resolution"], "remote")
        self.assertEqual(json.loads(path.read_text())["resolution"], "remote")
        sdir = next((self.root_b / "agents/main/sessions").glob(f"*_{SID}"))
        import hashlib
        self.assertEqual(hashlib.sha256((sdir / "messages.jsonl").read_bytes()).hexdigest(), record["remoteHash"])

    def test_resolve_take_local_force_exports_and_claims(self):
        path, record = self.diverge_and_record()
        result = json.loads(run_cli(
            SYNCD, "--config", self.config_b, "--state", self.state_b,
            "resolve", SID, "--take", "local",
        ).stdout)
        self.assertEqual(result["resolution"], "local")
        self.assertEqual(json.loads(path.read_text())["resolution"], "local")
        lock = json.loads((self.shared / "locks" / f"{SID}.json").read_text())
        self.assertEqual(lock["ownerDeviceId"], "device-b")
        expected = self.shared / "bundles" / SID / f"device-b-{record['localHash'][:16]}.tgz"
        self.assertTrue(expected.exists())


if __name__ == "__main__":
    unittest.main()
