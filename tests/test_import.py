from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.common import (
    SYNC, create_fixture, db_snapshot, empty_target, make_v1_bundle, run_cli, tar_json,
)


SID = "sessionABC123456"


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aside-sync-tests-")
        self.base = Path(self.tmp.name)
        self.source = create_fixture(self.base / "source", SID)
        self.target = empty_target(self.base / "target")
        self.bundle = self.base / "bundle.tgz"
        run_cli(
            SYNC, "--aside-root", self.source, "export-bundle", SID,
            "--output", self.bundle, "--source-device-id", "device-a",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def import_bundle(self, *extra, check=True, bundle=None):
        return run_cli(
            SYNC, "--aside-root", self.target, "import-bundle", bundle or self.bundle,
            *extra, check=check,
        )

    def row(self, sid=SID):
        con = sqlite3.connect(self.target / "state.db")
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        con.close()
        return dict(row) if row else None

    def test_fresh_import_forces_safe_fields_and_preserves_tab_target(self):
        result = self.import_bundle()
        output = json.loads(result.stdout)
        row = self.row()
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["queued_messages"], "[]")
        self.assertIsNone(row["suspension"])
        self.assertIsNone(row["active_tab_target_id"])
        self.assertIsNone(row["browser_binding"])
        self.assertEqual(row["latest_compaction_message_offset"], 0)
        self.assertEqual(row["ephemeral"], 0)
        self.assertIsNone(row["archived_at"])
        tool = json.loads(row["tool_state"])
        self.assertEqual(tool["todo"], {"todos": []})
        self.assertEqual(tool["subagent"], {"subagents": []})
        self.assertEqual(tool["bash"]["cwd"], output["sessionDir"])
        con = sqlite3.connect(self.target / "state.db")
        tab = con.execute("SELECT session_id,target_id FROM session_tabs").fetchone()
        con.close()
        self.assertEqual(tab, (SID, "tab:TARGET-VERBATIM"))

    def test_update_existing_replaces_rows_and_creates_backup(self):
        self.import_bundle()
        dest = next((self.target / "agents/main/sessions").glob(f"*_{SID}"))
        (dest / "messages.jsonl").write_text('{"local":"old"}\n', encoding="utf-8")
        con = sqlite3.connect(self.target / "state.db")
        con.execute("UPDATE sessions SET status='idle', title='local old' WHERE id=?", (SID,))
        con.execute("UPDATE session_runs SET user_message='local old' WHERE session_id=?", (SID,))
        con.commit()
        con.close()
        result = self.import_bundle("--update-existing", "--force")
        data = json.loads(result.stdout)
        self.assertEqual(self.row()["title"], "Fixture session")
        backup = Path(data["backupDir"])
        self.assertTrue((backup / "state.db.before-import").exists())
        self.assertTrue(any(p.name.endswith(SID) for p in backup.iterdir() if p.is_dir()))
        con = sqlite3.connect(self.target / "state.db")
        self.assertEqual(con.execute("SELECT user_message FROM session_runs").fetchone()[0], "hello")
        con.close()

    def test_running_session_refused_without_backup_or_change(self):
        self.import_bundle()
        con = sqlite3.connect(self.target / "state.db")
        con.execute("UPDATE sessions SET status='running' WHERE id=?", (SID,))
        con.commit()
        con.close()
        before = db_snapshot(self.target)
        result = self.import_bundle("--update-existing", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("running", result.stderr)
        self.assertEqual(before, db_snapshot(self.target))

    def test_v1_refused_and_scrub_moves_it_to_configured_trash(self):
        old = self.base / "sync" / "bundles" / "old.tgz"
        old.parent.mkdir(parents=True)
        make_v1_bundle(old)
        result = self.import_bundle(check=False, bundle=old)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("v1", result.stderr)
        trash = self.base / "Trash"
        scrub = run_cli(SYNC, "scrub-v1", self.base / "sync", "--trash-dir", trash)
        self.assertEqual(json.loads(scrub.stdout)["moved"], 1)
        self.assertFalse(old.exists())
        self.assertEqual(len(list(trash.iterdir())), 1)

    def test_schema_mismatch_ignores_unknown_bundle_field_with_warning(self):
        # Repack db.json with a future whitelisted-session field; hashes do not cover db.json.
        unpack = self.base / "unpack"
        with tarfile.open(self.bundle, "r:gz") as tf:
            tf.extractall(unpack)
        db = json.loads((unpack / "db.json").read_text(encoding="utf-8"))
        db["session"]["future_column"] = "future"
        (unpack / "db.json").write_text(json.dumps(db), encoding="utf-8")
        future = self.base / "future.tgz"
        with tarfile.open(future, "w:gz") as tf:
            for p in sorted(unpack.rglob("*")):
                if p.is_file():
                    tf.add(p, arcname=str(p.relative_to(unpack)))
        result = self.import_bundle(bundle=future)
        self.assertIn("future_column", result.stderr)
        self.assertIsNotNone(self.row())

    def test_fork_import_rewrites_relations_but_not_message_content(self):
        self.import_bundle()
        result = self.import_bundle("--as-new-session")
        data = json.loads(result.stdout)
        new_sid = data["imported"]
        self.assertNotEqual(new_sid, SID)
        self.assertEqual(len(new_sid), 16)
        row = self.row(new_sid)
        self.assertEqual(row["branched_from"], SID)
        self.assertEqual(row["title"], "Fixture session (conflict from device-a)")
        con = sqlite3.connect(self.target / "state.db")
        self.assertEqual(con.execute("SELECT DISTINCT session_id FROM session_runs WHERE session_id=?", (new_sid,)).fetchone()[0], new_sid)
        target = con.execute("SELECT target_id FROM session_tabs WHERE session_id=?", (new_sid,)).fetchone()[0]
        con.close()
        self.assertEqual(target, "tab:TARGET-VERBATIM")
        dest = Path(data["sessionDir"])
        self.assertIn("tab:TARGET-VERBATIM", (dest / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn(new_sid, (dest / "messages.jsonl").read_text(encoding="utf-8"))

    def test_dry_run_touches_nothing(self):
        before = db_snapshot(self.target)
        before_dirs = sorted(str(p.relative_to(self.target)) for p in self.target.rglob("*"))
        result = self.import_bundle("--dry-run")
        plan = json.loads(result.stdout)
        self.assertTrue(plan["dryRun"])
        self.assertGreater(len(plan["actions"]), 0)
        self.assertEqual(before, db_snapshot(self.target))
        self.assertEqual(before_dirs, sorted(str(p.relative_to(self.target)) for p in self.target.rglob("*")))

    def test_hash_tampering_is_refused_before_any_target_change(self):
        unpack = self.base / "tampered-unpack"
        with tarfile.open(self.bundle, "r:gz") as tf:
            tf.extractall(unpack)
        with (unpack / "files/messages.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"attacker":true}\n')
        tampered = self.base / "tampered.tgz"
        with tarfile.open(tampered, "w:gz") as tf:
            for path in sorted(unpack.rglob("*")):
                if path.is_file():
                    tf.add(path, arcname=str(path.relative_to(unpack)))
        before = db_snapshot(self.target)
        result = self.import_bundle(bundle=tampered, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("messages.jsonl", result.stderr)
        self.assertEqual(before, db_snapshot(self.target))

    def test_unsafe_manifest_session_directory_is_refused(self):
        unpack = self.base / "unsafe-unpack"
        with tarfile.open(self.bundle, "r:gz") as tf:
            tf.extractall(unpack)
        manifest = json.loads((unpack / "manifest.json").read_text(encoding="utf-8"))
        manifest["sessionDirName"] = "../../escaped_" + SID
        (unpack / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        unsafe = self.base / "unsafe.tgz"
        with tarfile.open(unsafe, "w:gz") as tf:
            for path in sorted(unpack.rglob("*")):
                if path.is_file():
                    tf.add(path, arcname=str(path.relative_to(unpack)))
        result = self.import_bundle(bundle=unsafe, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sessionDirName", result.stderr)
        self.assertFalse((self.base / f"escaped_{SID}").exists())


if __name__ == "__main__":
    unittest.main()
