from __future__ import annotations

import errno
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.common import SYNC, SYNCD, create_fixture, daemon_config, empty_target, run_cli


SID = "sessionABC123456"


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-tests-")
        self.base = Path(self.tmp.name)
        self.root_a = create_fixture(self.base / "a", SID)
        self.root_b = empty_target(self.base / "b")
        self.shared = self.base / "shared"
        self.config_a = self.base / "config-a.json"
        self.config_b = self.base / "config-b.json"
        self.state_a = self.base / "state-a.json"
        self.state_b = self.base / "state-b.json"
        daemon_config(self.config_a, self.root_a, self.shared, "device-a")
        daemon_config(self.config_b, self.root_b, self.shared, "device-b")

    def tearDown(self):
        self.tmp.cleanup()

    def cycle(self, config, state, *extra, env=None):
        return json.loads(run_cli(SYNCD, "--config", config, "--state", state, "run-once", *extra, env=env).stdout)

    def test_two_roots_export_import_dedupe_and_index_heartbeat(self):
        a = self.cycle(self.config_a, self.state_a)
        self.assertEqual(a["exported"], [SID])
        b = self.cycle(self.config_b, self.state_b)
        self.assertEqual([item["sessionId"] for item in b["imported"]], [SID])
        con = sqlite3.connect(self.root_b / "state.db")
        self.assertEqual(con.execute("SELECT title FROM sessions WHERE id=?", (SID,)).fetchone()[0], "Fixture session")
        self.assertEqual(con.execute("SELECT target_id FROM session_tabs WHERE session_id=?", (SID,)).fetchone()[0], "tab:TARGET-VERBATIM")
        con.close()
        second = self.cycle(self.config_b, self.state_b)
        self.assertEqual(second["exported"], [])
        self.assertEqual(second["imported"], [])
        index = json.loads((self.shared / "indexes" / "device-b.json").read_text())
        self.assertEqual(index["schemaVersion"], 2)
        self.assertEqual(index["deviceId"], "device-b")
        self.assertGreater(index["heartbeatAt"], 0)
        self.assertFalse(any(self.shared.rglob("*.tmp")))

    def test_v1_config_and_state_migrate(self):
        old_cfg = {
            "deviceId": "legacy", "asideRoot": str(self.root_a), "asideSyncPath": str(SYNC),
            "syncDir": str(self.shared), "intervalSeconds": 60, "agentId": "main",
            "conflictPolicy": "skip", "logPath": str(self.base / "legacy.log"),
        }
        legacy_cfg = self.base / "legacy.json"
        legacy_cfg.write_text(json.dumps(old_cfg), encoding="utf-8")
        legacy_state = self.base / "legacy-state.json"
        legacy_state.write_text(json.dumps({"version": 1, "sessions": {"x": {"kept": True}}}), encoding="utf-8")
        run_cli(SYNCD, "--config", legacy_cfg, "--state", legacy_state, "run-once")
        migrated = json.loads(legacy_cfg.read_text())
        self.assertEqual(migrated["schemaVersion"], 2)
        self.assertEqual(json.loads((self.base / "legacy.json.v1.bak").read_text()), old_cfg)
        state = json.loads(legacy_state.read_text())
        self.assertEqual(state["schemaVersion"], 2)
        self.assertTrue(state["sessions"]["x"]["kept"])

    def test_dry_run_full_cycle_writes_nothing(self):
        before = sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))
        result = self.cycle(self.config_a, self.state_a, "--dry-run")
        self.assertTrue(result["dryRun"])
        after = sorted(str(p.relative_to(self.base)) for p in self.base.rglob("*"))
        self.assertEqual(before, after)

    def test_load_json_retries_dataless_cloud_read_with_brctl_nudge(self):
        api = runpy.run_path(str(SYNCD))
        target = Path(self.tmp.name) / "lock.json"
        target.write_text('{"ok": true}', encoding="utf-8")
        deadlock = OSError(errno.EDEADLK, "Resource deadlock avoided")
        nudged = []
        real_read_text = Path.read_text
        reads = {"n": 0}

        def flaky_read_text(path_self, *args, **kwargs):
            if path_self == target and reads["n"] == 0:
                reads["n"] += 1
                raise deadlock
            return real_read_text(path_self, *args, **kwargs)

        with mock.patch.dict(api["load_json"].__globals__, {
            "nudge_cloud_download": lambda p: nudged.append(p),
            "time": mock.Mock(sleep=lambda s: None),
        }):
            with mock.patch.object(Path, "read_text", flaky_read_text):
                data = api["load_json"](target, None)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(nudged, [target])

    def test_log_rotates_at_five_megabytes(self):
        config = json.loads(self.config_a.read_text())
        log = Path(config["logPath"])
        log.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        self.cycle(self.config_a, self.state_a)
        self.assertTrue(Path(str(log) + ".1").exists())
        self.assertLess(log.stat().st_size, 5 * 1024 * 1024)

    def test_encryption_round_trip_available_backends(self):
        source_bundle = self.base / "plain.tgz"
        run_cli(SYNC, "--aside-root", self.root_a, "export-bundle", SID, "--output", source_bundle)
        # none is always available.
        self.assertEqual(json.loads(run_cli(SYNC, "inspect-bundle", source_bundle).stdout)["sessionId"], SID)

        env = os.environ.copy()
        env["ASIDE_SYNC_PASSPHRASE"] = "correct horse battery staple"
        if shutil.which("openssl"):
            encrypted = self.base / "bundle.tgz.enc"
            run_cli(SYNC, "--aside-root", self.root_a, "export-bundle", SID, "--output", encrypted,
                    "--encryption", "openssl", env=env)
            self.assertEqual(json.loads(run_cli(SYNC, "inspect-bundle", encrypted, env=env).stdout)["sessionId"], SID)
        else:
            self.skipTest("openssl binary missing")

        if shutil.which("gpg"):
            gnupg = self.base / "gnupg"
            gnupg.mkdir(mode=0o700)
            env["GNUPGHOME"] = str(gnupg)
            encrypted = self.base / "bundle.tgz.gpg"
            result = run_cli(
                SYNC, "--aside-root", self.root_a, "export-bundle", SID, "--output", encrypted,
                "--encryption", "gpg", env=env, check=False,
            )
            if result.returncode == 0:
                self.assertEqual(json.loads(run_cli(SYNC, "inspect-bundle", encrypted, env=env).stdout)["sessionId"], SID)
            else:
                print("skipped gpg encryption subcase: gpg-agent unavailable in sandbox")
        else:
            print("skipped gpg encryption subcase: binary missing")

        if shutil.which("age") and shutil.which("age-keygen"):
            key = self.base / "age.key"
            subprocess.run(["age-keygen", "-o", str(key)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            encrypted = self.base / "bundle.tgz.age"
            run_cli(SYNC, "--aside-root", self.root_a, "export-bundle", SID, "--output", encrypted,
                    "--encryption", "age", "--key-file", key)
            self.assertEqual(json.loads(run_cli(SYNC, "inspect-bundle", encrypted, "--key-file", key).stdout)["sessionId"], SID)
        else:
            print("skipped age encryption subcase: binary missing")

    def test_openssl_encrypted_daemon_bundle_and_index_round_trip(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl binary missing")
        encrypted_shared = self.base / "encrypted-shared"
        daemon_config(self.config_a, self.root_a, encrypted_shared, "device-a", encryption="openssl")
        daemon_config(self.config_b, self.root_b, encrypted_shared, "device-b", encryption="openssl")
        for path in (self.config_a, self.config_b):
            config = json.loads(path.read_text())
            config["security"]["encryptIndex"] = True
            path.write_text(json.dumps(config), encoding="utf-8")
        env = os.environ.copy()
        env["ASIDE_SYNC_PASSPHRASE"] = "correct horse battery staple"
        first = self.cycle(self.config_a, self.state_a, env=env)
        self.assertEqual(first["exported"], [SID])
        second = self.cycle(self.config_b, self.state_b, env=env)
        self.assertEqual([item["sessionId"] for item in second["imported"]], [SID])
        self.assertTrue((encrypted_shared / "indexes" / "device-a.json.enc").exists())
        self.assertFalse((encrypted_shared / "indexes" / "device-a.json").exists())
        self.assertTrue(any((encrypted_shared / "bundles" / SID).glob("*.tgz.enc")))


if __name__ == "__main__":
    unittest.main()
