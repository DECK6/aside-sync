from __future__ import annotations

import argparse
import contextlib
import errno
import io
import json
import os
import runpy
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.common import SYNCD, create_fixture, daemon_config, empty_target, run_cli


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-status-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "동기화 작업"
        self.base.mkdir()
        self.root = create_fixture(self.base / "local", "statusSession123")
        self.shared = self.base / "shared"
        self.config = self.base / "config.json"
        self.state = self.base / "state.json"
        daemon_config(self.config, self.root, self.shared, "local")
        self.api = runpy.run_path(str(SYNCD))

    def command(self, *args, check=True):
        return run_cli(SYNCD, "--config", self.config, "--state", self.state, *args, check=check)

    def invoke(self, name, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = self.api[name](argparse.Namespace(config=str(self.config), state=str(self.state), **kwargs))
        return json.loads(output.getvalue()), result

    def test_recent_two_device_cycle_reports_success_and_check_exits_zero(self):
        self.command("run-once")
        target = empty_target(self.base / "peer")
        config = self.base / "peer-config.json"
        daemon_config(config, target, self.shared, "peer")
        run_cli(SYNCD, "--config", config, "--state", self.base / "peer-state.json", "run-once")
        self.command("run-once")
        report = json.loads(self.command("status", "--check").stdout)
        self.assertTrue(report["health"]["ok"])
        self.assertEqual(report["lastCycle"]["skippedByReason"], {})
        self.assertEqual(report["lastCycle"]["outcome"], "completed")
        peer = json.loads((self.base / "peer-state.json").read_text())["lastCycle"]
        self.assertEqual(peer["importedCount"], 1)
        self.assertGreater(peer["finishedAt"], 0)

    def test_status_reports_unreadable_index_with_download_guidance(self):
        self.shared.mkdir()
        with mock.patch.dict(self.api["status"].__globals__, {"read_indexes": lambda config, **kwargs: ([], ["peer.json"])}):
            report, _ = self.invoke("status", check=False)
        self.assertEqual(report["unreadableIndexes"], ["peer.json"])
        issue = next(x for x in report["health"]["issues"] if x["code"] == "index-unreadable")
        self.assertIn("download", issue["hint"].lower())
        self.assertFalse(report["health"]["ok"])

    def test_failed_export_is_persisted_without_error_contents(self):
        def fail(*args, **kwargs):
            raise self.api["DaemonError"]("SECRET_FROM_ERROR: Resource deadlock avoided")
        with mock.patch.dict(self.api["run_once"].__globals__, {"core_command": fail}):
            self.invoke("run_once", dry_run=False)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["lastCycle"]["skippedByReason"], {"export-failed": 1})
        self.assertTrue(state["lastCycle"]["cloudFilesUnavailable"])
        self.assertNotIn("SECRET_FROM_ERROR", json.dumps(state))
        result = self.command("status", "--check", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cloud-files-unavailable", [x["code"] for x in json.loads(result.stdout)["health"]["issues"]])

    def test_missing_or_stale_cycles_and_peers_are_not_healthy(self):
        report = json.loads(self.command("status").stdout)
        self.assertIn("no-cycle", [x["code"] for x in report["health"]["issues"]])
        self.command("run-once")
        state = json.loads(self.state.read_text())
        state["lastCycle"]["finishedAt"] = int(time.time()) - 3600
        self.state.write_text(json.dumps(state), encoding="utf-8")
        (self.shared / "indexes/peer.json").write_text(json.dumps({
            "schemaVersion": 2, "deviceId": "peer", "heartbeatAt": int(time.time()) - 3600, "sessions": [],
        }), encoding="utf-8")
        result = self.command("status", "--check", check=False)
        self.assertEqual(result.returncode, 1)
        codes = [x["code"] for x in json.loads(result.stdout)["health"]["issues"]]
        self.assertIn("cycle-stale", codes)
        self.assertIn("peer-stale", codes)

    def test_native_sync_setting_is_informational_and_never_changed(self):
        settings = self.root / "settings.json"
        for enabled in (False, True):
            settings.write_text(json.dumps({"experimental": {"sync": enabled}, "secret": "MUST_NOT_LEAK"}), encoding="utf-8")
            before = {p: p.read_bytes() for p in (settings, self.config)}
            result = self.command("status")
            report = json.loads(result.stdout)
            self.assertIs(report["asideExperimentalSync"]["enabled"], enabled)
            self.assertNotIn("MUST_NOT_LEAK", result.stdout)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            self.assertFalse(self.state.exists())

    def test_dry_run_does_not_replace_last_completed_cycle(self):
        self.command("run-once")
        before = self.state.read_bytes()
        self.command("run-once", "--dry-run")
        self.assertEqual(before, self.state.read_bytes())

    def test_fatal_cycle_replaces_previous_success_in_status(self):
        self.command("run-once")
        (self.shared / "indexes/broken.json").write_text("invalid json", encoding="utf-8")
        result = self.command("run-once", check=False)
        self.assertEqual(result.returncode, 1)
        cycle = json.loads(self.state.read_text())["lastCycle"]
        self.assertEqual(cycle["outcome"], "failed")
        result = self.command("status", "--check", check=False)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report["unreadableIndexes"], ["broken.json"])
        self.assertIn("cycle-failed", [x["code"] for x in report["health"]["issues"]])

    def test_unavailable_conflict_file_does_not_hide_the_status_report(self):
        path = self.shared / "conflicts/pending.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        read = self.api["load_json"]
        def unavailable(target, default):
            if target == path:
                raise OSError(errno.EDEADLK, "Resource deadlock avoided")
            return read(target, default)
        with mock.patch.dict(self.api["status"].__globals__, {"load_json": unavailable}):
            report, code = self.invoke("status", check=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["unreadableConflicts"], ["pending.json"])
        self.assertIn("conflict-unreadable", [x["code"] for x in report["health"]["issues"]])

    def test_shared_folder_permission_failure_is_not_reported_as_missing_peer(self):
        self.shared.mkdir()
        original = os.listdir
        def denied(path):
            if Path(path) == self.shared:
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return original(path)
        with mock.patch.object(os, "listdir", side_effect=denied):
            report, code = self.invoke("status", check=True)
        self.assertEqual(code, 1)
        codes = [x["code"] for x in report["health"]["issues"]]
        self.assertIn("sync-folder-unavailable", codes)
        self.assertNotIn("no-peer", codes)
