from __future__ import annotations

import argparse
import contextlib
import io
import json
import plistlib
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.common import SYNCD


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-tests-")
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scripted_setup_writes_v2_choices_and_starts_nothing(self):
        config = self.base / "config.json"
        state = self.base / "state.json"
        sync_dir = self.base / "shared" / "AsideSync"
        api = runpy.run_path(str(SYNCD))
        custom_folder_choice = str(len(api["detected_sync_roots"]()) + 1)
        answers = "\n".join([
            custom_folder_choice,
            str(sync_dir),       # custom shared folder
            "studio-test",      # device id
            "2",                # sessions after date
            "2026-01-01",
            "y",                # exclude errored
            "n",                # keep interrupted
            "3",                # artifacts + attachments
            "1",                # no encryption (trusted local test folder)
            "3",                # LaunchAgent instructions, but do not start
            "y",                # confirm
            "",
        ])
        result = subprocess.run(
            [str(SYNCD), "--config", str(config), "--state", str(state), "setup"],
            input=answers, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        data = json.loads(config.read_text())
        self.assertEqual(data["schemaVersion"], 2)
        self.assertEqual(data["deviceId"], "studio-test")
        self.assertEqual(data["syncDir"], str(sync_dir.resolve()))
        self.assertEqual(data["scope"], {
            "mode": "after", "afterDate": "2026-01-01", "sessionIds": [],
            "excludeStatuses": ["errored"],
        })
        self.assertEqual(data["files"], {"artifacts": True, "attachments": True})
        self.assertEqual(data["security"]["encryption"], "none")
        self.assertFalse(data["security"]["encryptIndex"])
        self.assertIn("aside-syncd run-once", result.stdout)
        self.assertIn("aside-syncd install-launchagent", result.stdout)
        self.assertIn("aside-syncd uninstall", result.stdout)
        self.assertFalse(state.exists())
        self.assertFalse(any(self.base.rglob("*.plist")))
        self.assertFalse(any(self.base.rglob("*.log")))

    def test_wizard_accepts_user_passphrase_for_openssl_backend(self):
        api = runpy.run_path(str(SYNCD))
        config = self.base / "config.json"
        sync_dir = self.base / "shared" / "AsideSync"
        custom_folder_choice = str(len(api["detected_sync_roots"]()) + 1)
        answers = iter([
            custom_folder_choice, str(sync_dir), "studio-test",
            "1",                 # all sessions
            "n", "n",            # keep errored/interrupted
            "",                  # default file scope
            "2",                 # encrypt bundles
            "same-on-both-devices", "same-on-both-devices",
            "1",                 # manual automation
            "y",                 # confirm
        ])
        stored = {}
        fake_sys = mock.Mock(platform="darwin", stdin=mock.Mock(isatty=lambda: False))
        which = lambda name: "/usr/bin/openssl" if name == "openssl" else None
        with mock.patch.dict(api["setup_wizard"].__globals__, {
            "sys": fake_sys,
            "shutil": mock.Mock(which=which),
            "macos_store_keychain_passphrase": lambda service, value: stored.update({service: value}),
        }):
            with mock.patch("builtins.input", side_effect=lambda prompt="": next(answers)):
                with contextlib.redirect_stdout(io.StringIO()):
                    api["setup_wizard"](argparse.Namespace(config=str(config), state=str(self.base / "state.json")))
        self.assertEqual(stored, {"aside-syncd": "same-on-both-devices"})
        data = json.loads(config.read_text())
        self.assertEqual(data["security"]["encryption"], "openssl")

    def test_encryption_toggle_off_and_on(self):
        api = runpy.run_path(str(SYNCD))
        config_path = self.base / "config.json"
        config = api["default_config"]()
        config.update({"deviceId": "test", "syncDir": str(self.base / "shared")})
        config["security"].update({"encryption": "openssl", "encryptIndex": True})
        api["save_json"](config_path, config)

        with contextlib.redirect_stdout(io.StringIO()):
            api["encryption_toggle"](argparse.Namespace(config=str(config_path), mode="off", index=False))
        data = json.loads(config_path.read_text())
        self.assertEqual(data["security"]["encryption"], "none")
        self.assertFalse(data["security"]["encryptIndex"])

        stored = {}
        answers = iter(["shared-secret", "shared-secret"])
        fake_sys = mock.Mock(platform="darwin", stdin=mock.Mock(isatty=lambda: False))
        which = lambda name: "/usr/bin/openssl" if name == "openssl" else None
        with mock.patch.dict(api["encryption_toggle"].__globals__, {
            "sys": fake_sys,
            "shutil": mock.Mock(which=which),
            "macos_store_keychain_passphrase": lambda service, value: stored.update({service: value}),
        }):
            with mock.patch("builtins.input", side_effect=lambda prompt="": next(answers)):
                with contextlib.redirect_stdout(io.StringIO()):
                    api["encryption_toggle"](argparse.Namespace(config=str(config_path), mode="on", index=True))
        data = json.loads(config_path.read_text())
        self.assertEqual(data["security"]["encryption"], "openssl")
        self.assertTrue(data["security"]["encryptIndex"])
        self.assertEqual(stored, {"aside-syncd": "shared-secret"})

    def test_launchagent_uses_start_interval_run_once_and_mocked_launchctl(self):
        api = runpy.run_path(str(SYNCD))
        config_path = self.base / "config.json"
        state_path = self.base / "state.json"
        plist_path = self.base / "LaunchAgents" / "com.deck.aside-syncd.plist"
        trash = self.base / "Trash"
        config = api["default_config"]()
        config.update({
            "deviceId": "test", "syncDir": str(self.base / "shared"),
            "asideRoot": str(self.base / "aside/u/0"), "asideSyncPath": str(self.base / "aside-sync"),
            "logPath": str(self.base / "daemon.log"), "intervalSeconds": 321,
        })
        api["save_json"](config_path, config)
        args = argparse.Namespace(
            config=str(config_path), state=str(state_path), label="com.deck.aside-syncd",
            output=str(plist_path), dry_run=False, trash_dir=str(trash),
        )
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.dict(api["install_launchagent"].__globals__, {"sys": mock.Mock(platform="darwin", executable="/usr/bin/python3")}):
            with mock.patch.object(api["subprocess"], "run", return_value=completed) as launchctl:
                with contextlib.redirect_stdout(io.StringIO()):
                    api["install_launchagent"](args)
        with plist_path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["StartInterval"], 321)
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertNotIn("KeepAlive", plist)
        self.assertIn("run-once", plist["ProgramArguments"])
        self.assertNotIn("daemon", plist["ProgramArguments"])
        calls = [call.args[0] for call in launchctl.call_args_list]
        self.assertTrue(any("bootstrap" in call for call in calls))
        self.assertTrue(any("enable" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
