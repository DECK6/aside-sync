from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from tests.common import SYNC, SYNCD


class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.core = runpy.run_path(str(SYNC))
        self.daemon = runpy.run_path(str(SYNCD))

    def test_extensionless_core_uses_current_python(self):
        config = self.daemon["default_config"]()
        with mock.patch.object(sys, "platform", "win32"), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}", "")) as call:
            self.daemon["core_command"](config, ["status"])
        self.assertEqual(call.call_args.args[0][:2], [sys.executable, config["asideSyncPath"]])

    def test_windows_cloud_folder_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("iCloudDrive", "OneDrive - Company", "Dropbox"):
                (root/name).mkdir()
            with mock.patch.object(sys, "platform", "win32"), mock.patch.object(Path, "home", return_value=root), mock.patch.dict(os.environ, {"OneDriveCommercial": str(root/"OneDrive - Company")}):
                found = self.daemon["detected_sync_roots"]()
            self.assertIn(root/"iCloudDrive", found)
            self.assertIn(root/"OneDrive - Company", found)

    def test_task_xml_quotes_paths_and_runs_without_password(self):
        args = argparse.Namespace(config=str(Path.cwd()/"테스트 User"/"config.json"), state=str(Path.cwd()/"테스트 User"/"state.json"))
        text = self.daemon["windows_task_xml"](self.daemon["default_config"](), args, r"PC\user")
        root = ET.fromstring(text)
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        def value(path): return root.find(path, ns).text
        self.assertEqual(value("t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(value("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(value("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval"), "PT300S")
        self.assertIn(subprocess.list2cmdline([args.config]), value("t:Actions/t:Exec/t:Arguments"))
        self.assertEqual(value("t:Actions/t:Exec/t:Command"), sys.executable)
        self.assertNotIn("Password", text)

    def test_task_install_dry_run_has_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.daemon["default_config"]()
            config["syncDir"] = str(Path(tmp)/"shared")
            path = Path(tmp)/"config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            args = self.daemon["build_parser"]().parse_args(["--config", str(path), "install-scheduled-task", "--dry-run"])
            before = list(Path(tmp).iterdir())
            with mock.patch.object(subprocess, "run") as call, mock.patch("builtins.print"):
                args.func(args)
            call.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), before)

    def test_windows_unsafe_bundle_paths_rejected_on_every_platform(self):
        for path in ("C:/escape", "files/artifacts/x:stream", "files/attachments/NUL.txt", "files/artifacts/trailing. ", "files/artifacts/aux"):
            with self.subTest(path=path), self.assertRaises(self.core["SyncError"]):
                self.core["safe_member_name"](path)

    def test_symmetric_env_does_not_require_keychain(self):
        config = self.daemon["default_config"]()
        config["security"]["encryption"] = "openssl"
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {"ASIDE_SYNC_PASSPHRASE": "test-only"}):
            self.assertEqual(self.daemon["command_env"](config)["ASIDE_SYNC_PASSPHRASE"], "test-only")

    def test_recycle_uses_windows_recycle_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"keep.txt"
            path.write_text("keep", encoding="utf-8")
            with mock.patch.object(sys, "platform", "win32"), mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as call:
                self.core["move_to_trash"](path)
            self.assertTrue(call.called)
            self.assertIn("SendToRecycleBin", str(call.call_args))
