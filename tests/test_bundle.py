from __future__ import annotations

import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.common import SYNC, create_fixture, run_cli, sha256, tar_json


SID = "sessionABC123456"
EXCLUDED = {
    "tool_state", "browser_binding", "active_tab_target_id", "queued_messages",
    "steering_messages", "suspension", "runtime_config", "trigger",
    "trigger_idempotency_key", "routine_id", "channel_route_key", "read_at",
    "latest_compaction_message_offset", "ephemeral", "archived_at",
}
EXPECTED = {
    "id", "agent_id", "parent_id", "branched_from", "title", "status",
    "system_prompt", "model", "permission_mode", "permission", "context_window",
    "cwd", "incognito", "created_at", "updated_at",
}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-tests-")
        self.base = Path(self.tmp.name)
        self.root = create_fixture(self.base, SID)
        self.bundle = self.base / "out" / "bundle.tgz"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self, *extra):
        return run_cli(
            SYNC, "--aside-root", self.root, "export-bundle", SID,
            "--output", self.bundle, "--source-device-id", "device-a", *extra,
        )

    def test_export_members_whitelist_hashes_and_no_db(self):
        self.export()
        with tarfile.open(self.bundle, "r:gz") as tf:
            members = {m.name for m in tf.getmembers() if m.isfile()}
            self.assertEqual(
                members,
                {"manifest.json", "db.json", "files/messages.jsonl", "files/artifacts/result.txt"},
            )
            self.assertFalse(any(name.endswith(".db") for name in members))
            msg = tf.extractfile("files/messages.jsonl").read()
        db = tar_json(self.bundle, "db.json")
        self.assertEqual(set(db["session"]), EXPECTED)
        self.assertTrue(EXCLUDED.isdisjoint(db["session"]))
        self.assertNotIn("BOT_TOKEN_MUST_NOT_LEAK", self.bundle.read_bytes().decode("latin1"))
        manifest = tar_json(self.bundle, "manifest.json")
        self.assertEqual(manifest["format"], "aside-session-bundle")
        self.assertEqual(manifest["schemaVersion"], 2)
        import hashlib
        self.assertEqual(manifest["messages"]["sha256"], hashlib.sha256(msg).hexdigest())
        self.assertEqual(manifest["messages"]["lines"], 3)
        self.assertEqual(manifest["dbCounts"], {"runs": 1, "tabs": 1})
        self.assertEqual(len(manifest["files"]), 1)

    def test_attachment_opt_in_and_tmp_always_excluded(self):
        self.export("--include-attachments")
        with tarfile.open(self.bundle, "r:gz") as tf:
            names = {m.name for m in tf.getmembers()}
        self.assertIn("files/attachments/upload.txt", names)
        self.assertFalse(any("tmp" in Path(n).parts for n in names))

    def test_jsonl_validation_fails_without_publishing_partial_bundle(self):
        msg = next((self.root / "agents/main/sessions").glob(f"*_{SID}")) / "messages.jsonl"
        msg.write_text('{"ok":true}\nnot-json\n', encoding="utf-8")
        result = self.export(check=False) if False else run_cli(
            SYNC, "--aside-root", self.root, "export-bundle", SID,
            "--output", self.bundle, "--source-device-id", "device-a", check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("line 2", result.stderr)
        self.assertFalse(self.bundle.exists())
        staging = self.bundle.parent / ".staging"
        self.assertFalse(staging.exists() and any(staging.iterdir()))

    def test_inspect_bundle_prints_manifest(self):
        self.export()
        result = run_cli(SYNC, "inspect-bundle", self.bundle)
        data = json.loads(result.stdout)
        self.assertEqual(data["sessionId"], SID)
        self.assertEqual(data["schemaVersion"], 2)

    def test_export_rejects_symlinked_content(self):
        secret = self.base / "outside-secret.txt"
        secret.write_text("must stay outside", encoding="utf-8")
        artifacts = next((self.root / "agents/main/sessions").glob(f"*_{SID}")) / "artifacts"
        (artifacts / "outside-link.txt").symlink_to(secret)
        result = run_cli(
            SYNC, "--aside-root", self.root, "export-bundle", SID,
            "--output", self.bundle, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr)
        self.assertFalse(self.bundle.exists())


if __name__ == "__main__":
    unittest.main()
