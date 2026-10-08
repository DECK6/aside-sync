from __future__ import annotations

import json
from contextlib import closing
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.common import SYNC, SYNCD, create_fixture, empty_target, run_cli, session_directory, tar_json, daemon_config

SID = "latestSession1234"


def latest_schema(root):
    with closing(sqlite3.connect(root / "state.db")) as con, con:
        con.execute("ALTER TABLE session_runs RENAME TO session_turns")
        for definition in (
            "resume_attempts INTEGER NOT NULL DEFAULT 0", "turn_id TEXT",
            "final_started_at INTEGER", "body_version INTEGER NOT NULL DEFAULT 0",
            "steering_messages TEXT NOT NULL DEFAULT '[]'",
            "has_body INTEGER NOT NULL DEFAULT 1", "trailing_messages TEXT NOT NULL DEFAULT '[]'",
            "future_secret TEXT DEFAULT 'NEVER_EXPORT'",
        ):
            con.execute("ALTER TABLE session_turns ADD COLUMN " + definition)
        con.execute("ALTER TABLE sessions ADD COLUMN transcript_revision INTEGER NOT NULL DEFAULT 0")
        con.execute("ALTER TABLE sessions ADD COLUMN harness_id TEXT NOT NULL DEFAULT 'v1'")
        con.execute("UPDATE session_turns SET turn_id='turn-stable', body_version=7, final_started_at=2")
        con.execute("UPDATE sessions SET transcript_revision=12")
    return root


class LatestSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_schema_matrix_preserves_messages_and_turns(self):
        for src in ("legacy", "current", "latest"):
            for dst in ("legacy", "current", "latest"):
                with self.subTest(source=src, destination=dst):
                    case = self.base / (src + "-" + dst)
                    source = create_fixture(case / "source", SID, schema="current" if src == "latest" else src)
                    target = empty_target(case / "target", schema="current" if dst == "latest" else dst)
                    if src == "latest": latest_schema(source)
                    if dst == "latest": latest_schema(target)
                    bundle = case / "bundle.tgz"
                    run_cli(SYNC, "--aside-root", source, "export-bundle", SID, "--output", bundle)
                    data = tar_json(bundle, "db.json")
                    self.assertNotIn("future_secret", data["runs"][0])
                    if src == "latest":
                        self.assertEqual(data["runs"][0]["turn_id"], "turn-stable")
                        self.assertEqual(data["runs"][0]["body_version"], 7)
                        self.assertEqual(data["session"]["harness_id"], "v1")
                    run_cli(SYNC, "--aside-root", target, "import-bundle", bundle)
                    self.assertEqual((session_directory(source, SID)/"messages.jsonl").read_bytes(),
                                     (session_directory(target, SID)/"messages.jsonl").read_bytes())
                    with closing(sqlite3.connect(target/"state.db")) as con, con:
                        table = "session_turns" if dst == "latest" else "session_runs"
                        self.assertEqual(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 1)
                        if src == dst == "latest":
                            self.assertEqual(con.execute("SELECT turn_id,body_version FROM session_turns").fetchone(), ("turn-stable", 7))

    def test_latest_update_and_fork(self):
        source = latest_schema(create_fixture(self.base/"source", SID, schema="current"))
        target = latest_schema(create_fixture(self.base/"target", SID, schema="current"))
        with closing(sqlite3.connect(target/"state.db")) as con, con:
            con.execute("UPDATE sessions SET transcript_revision=20")
        bundle = self.base/"bundle.tgz"
        run_cli(SYNC, "--aside-root", source, "export-bundle", SID, "--output", bundle)
        run_cli(SYNC, "--aside-root", target, "import-bundle", bundle, "--update-existing")
        with closing(sqlite3.connect(target/"state.db")) as con, con:
            self.assertGreater(con.execute("SELECT transcript_revision FROM sessions").fetchone()[0], 20)
        fork = json.loads(run_cli(SYNC, "--aside-root", target, "import-bundle", bundle, "--as-new-session").stdout)
        with closing(sqlite3.connect(target/"state.db")) as con, con:
            self.assertEqual(con.execute("SELECT count(*) FROM session_turns WHERE session_id=?", (fork["imported"],)).fetchone()[0], 1)

    def test_bad_session_does_not_abort_other_exports(self):
        source = latest_schema(create_fixture(self.base/"source", SID, schema="current"))
        good_sid = "healthySession123"
        original_dir = session_directory(source, SID)
        shutil.copytree(original_dir, original_dir.with_name("2026-10-08_" + good_sid))
        with closing(sqlite3.connect(source/"state.db")) as con, con:
            cols = [x[1] for x in con.execute("pragma table_info(sessions)")]
            row = dict(zip(cols, con.execute("select * from sessions").fetchone()))
            row["id"] = good_sid
            con.execute(f"insert into sessions ({','.join(cols)}) values ({','.join('?' for _ in cols)})", [row[c] for c in cols])
        (session_directory(source, SID)/"messages.jsonl").write_text("broken json\n", encoding="utf-8")
        config, state = self.base/"config.json", self.base/"state.json"
        daemon_config(config, source, self.base/"shared", "device-a")
        result = json.loads(run_cli(SYNCD, "--config", config, "--state", state, "run-once").stdout)
        self.assertTrue(any(x["reason"] == "export-failed" for x in result["skipped"]))
        self.assertIn(good_sid, result["exported"])
        self.assertNotIn("lastExportedHash", json.loads(state.read_text())["sessions"][SID])

    def test_identical_peer_history_needs_no_duplicate_bundle(self):
        source = latest_schema(create_fixture(self.base/"source", SID, schema="current"))
        shared = self.base/"shared"
        (shared/"indexes").mkdir(parents=True)
        digest = hashlib.sha256((session_directory(source,SID)/"messages.jsonl").read_bytes()).hexdigest()
        (shared/"indexes/peer.json").write_text(json.dumps({"schemaVersion":2,"deviceId":"peer","sessions":[{"id":SID,"messagesSha256":digest}]}))
        config,state = self.base/"config.json", self.base/"state.json"
        daemon_config(config,source,shared,"local")
        result = json.loads(run_cli(SYNCD,"--config",config,"--state",state,"run-once").stdout)
        self.assertFalse(any(x["reason"] == "bundle-missing" for x in result["skipped"]))
        self.assertEqual(json.loads(state.read_text())["sessions"][SID]["lastImportedHash"],digest)
