from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SYNC = REPO / "bin" / "aside-sync"
SYNCD = REPO / "bin" / "aside-syncd"


def run_cli(script: Path, *args: object, check: bool = True, env=None):
    command = [sys.executable, "-X", "utf8", str(script), *(str(x) for x in args)]
    return subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
        env=env,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def session_directory(root: Path, sid: str, agent_id: str = "main") -> Path | None:
    for parent in (root / "sessions", root / "agents" / agent_id / "sessions"):
        if not parent.exists():
            continue
        matches = sorted(parent.glob(f"*_{sid}"))
        if matches:
            return matches[-1]
    return None


def create_fixture(
    base: Path, sid: str = "sessionABC123456", *, status: str = "idle", schema: str = "legacy",
) -> Path:
    if schema not in ("legacy", "current"):
        raise ValueError(f"unsupported fixture schema: {schema}")
    root = base / "aside" / "u" / "0"
    root.mkdir(parents=True)
    db = root / "state.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    if schema == "legacy":
        con.executescript(
            """
            CREATE TABLE agents (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, parent_id TEXT,
              branched_from TEXT, title TEXT, trigger TEXT,
              trigger_idempotency_key TEXT, routine_id TEXT, channel_route_key TEXT,
              status TEXT, system_prompt TEXT, model TEXT, permission_mode TEXT,
              permission TEXT, context_window INTEGER, queued_messages TEXT,
              steering_messages TEXT, cwd TEXT, suspension TEXT, tool_state TEXT,
              active_tab_target_id TEXT, incognito INTEGER DEFAULT 0,
              ephemeral INTEGER DEFAULT 0, runtime_config TEXT, read_at INTEGER,
              latest_compaction_message_offset INTEGER DEFAULT 0, archived_at INTEGER,
              created_at INTEGER, updated_at INTEGER, browser_binding TEXT,
              FOREIGN KEY(agent_id) REFERENCES agents(id)
            );
            """
        )
        con.execute("INSERT INTO agents VALUES ('main','Main')")
    else:
        con.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, parent_id TEXT, branched_from TEXT, title TEXT,
              trigger TEXT, trigger_idempotency_key TEXT, routine_id TEXT,
              channel_route_key TEXT, status TEXT, system_prompt TEXT, model TEXT,
              permission_mode TEXT, permission TEXT, context_window TEXT,
              queued_messages TEXT, steering_messages TEXT, cwd TEXT,
              suspension TEXT, tool_state TEXT, active_tab_target_id TEXT,
              incognito INTEGER DEFAULT 0, ephemeral INTEGER DEFAULT 0,
              runtime_config TEXT, read_at INTEGER,
              latest_compaction_message_offset INTEGER DEFAULT 0, archived_at INTEGER,
              created_at INTEGER, updated_at INTEGER, browser_binding TEXT,
              project_id TEXT
            );
            """
        )
    con.executescript(
        """
        CREATE TABLE session_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
          user_message TEXT, final_assistant_message TEXT, files_changed TEXT,
          token_usage TEXT, started_at INTEGER, last_message_timestamp INTEGER,
          finished_at INTEGER, aborted_at INTEGER, abort_reason TEXT,
          jsonl_read_offset INTEGER, jsonl_read_size INTEGER,
          UNIQUE(session_id, jsonl_read_offset),
          FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE TABLE session_tabs (
          id TEXT PRIMARY KEY, session_id TEXT, ownership TEXT, source TEXT,
          target_id TEXT, url TEXT, data TEXT, created_at INTEGER, updated_at INTEGER,
          UNIQUE(session_id, target_id),
          FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE TABLE channel_connections (id TEXT, bot_token TEXT);
        """
    )
    session = {
        "id": sid, "parent_id": None, "branched_from": None, "title": "Fixture session",
        "trigger": "secret-trigger", "trigger_idempotency_key": "secret-idempotency",
        "routine_id": "routine-secret", "channel_route_key": "channel-secret", "status": status,
        "system_prompt": "system", "model": "model-x", "permission_mode": "default",
        "permission": "ask", "context_window": 100000, "queued_messages": '["queued-secret"]',
        "steering_messages": '["steering-secret"]', "cwd": "/source/path",
        "suspension": "suspended-secret", "tool_state": '{"sensitive":"tool-state"}',
        "active_tab_target_id": "tab:live", "incognito": 0, "ephemeral": 0,
        "runtime_config": '{"secret":"runtime"}', "read_at": 111,
        "latest_compaction_message_offset": 99, "archived_at": None,
        "created_at": 1000, "updated_at": 2000, "browser_binding": '{"live":"browser"}',
    }
    if schema == "legacy":
        session = {"id": session.pop("id"), "agent_id": "main", **session}
    else:
        session["project_id"] = None
    fields = list(session)
    con.execute(
        f"INSERT INTO sessions ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        [session[field] for field in fields],
    )
    con.execute(
        """INSERT INTO session_runs
        (session_id,user_message,final_assistant_message,files_changed,token_usage,
         started_at,last_message_timestamp,finished_at,aborted_at,abort_reason,
         jsonl_read_offset,jsonl_read_size)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, "hello", "world", "[]", "{}", 1, 2, 3, None, None, 0, 50),
    )
    con.execute(
        "INSERT INTO session_tabs VALUES (?,?,?,?,?,?,?,?,?)",
        ("tab-row-1", sid, "owned", "browser", "tab:TARGET-VERBATIM", "https://example.test", "{}", 1, 2),
    )
    con.execute("INSERT INTO channel_connections VALUES ('conn','BOT_TOKEN_MUST_NOT_LEAK')")
    con.commit()
    con.close()

    parent = root / "agents" / "main" / "sessions" if schema == "legacy" else root / "sessions"
    sdir = parent / f"2026-07-20_{sid}"
    (sdir / "artifacts").mkdir(parents=True)
    (sdir / "attachments").mkdir()
    (sdir / "tmp").mkdir()
    lines = [
        {"type": "user-message-metadata", "attachments": [{"targetId": "tab:TARGET-VERBATIM"}]},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world", "id": "msg_123"},
    ]
    (sdir / "messages.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    (sdir / "artifacts" / "result.txt").write_text("artifact", encoding="utf-8")
    (sdir / "attachments" / "upload.txt").write_text("attachment", encoding="utf-8")
    (sdir / "tmp" / "secret.tmp").write_text("never export", encoding="utf-8")
    return root


def empty_target(base: Path, *, schema: str = "legacy") -> Path:
    root = create_fixture(base, sid="placeholder00000", schema=schema)
    con = sqlite3.connect(root / "state.db")
    con.execute("DELETE FROM session_runs")
    con.execute("DELETE FROM session_tabs")
    con.execute("DELETE FROM sessions")
    con.commit()
    con.close()
    sessions = root / "agents" / "main" / "sessions" if schema == "legacy" else root / "sessions"
    for child in sessions.iterdir():
        # Test fixtures are disposable tempfile data.
        import shutil
        shutil.rmtree(child)
    return root


def tar_json(bundle: Path, member: str):
    with tarfile.open(bundle, "r:gz") as tf:
        handle = tf.extractfile(member)
        assert handle is not None
        return json.loads(handle.read().decode("utf-8"))


def db_snapshot(root: Path):
    con = sqlite3.connect(root / "state.db")
    rows = {}
    for table in ("sessions", "session_runs", "session_tabs"):
        rows[table] = con.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    con.close()
    files = {}
    sessions = root / "sessions"
    if not sessions.exists():
        sessions = root / "agents" / "main" / "sessions"
    if sessions.exists():
        for path in sorted(p for p in sessions.rglob("*") if p.is_file()):
            files[str(path.relative_to(root))] = path.read_bytes()
    return rows, files


def make_v1_bundle(path: Path, sid: str = "old"):
    staging = path.parent / "v1-parts"
    staging.mkdir()
    (staging / "manifest.json").write_text(
        json.dumps({"format": "aside-session-bundle-v1", "sessionId": sid}), encoding="utf-8"
    )
    (staging / "source-state.db").write_bytes(b"credential leak")
    with tarfile.open(path, "w:gz") as tf:
        tf.add(staging / "manifest.json", arcname="manifest.json")
        tf.add(staging / "source-state.db", arcname="source-state.db")


def daemon_config(path: Path, root: Path, sync_dir: Path, device: str, *, policy: str = "record", encryption: str = "none") -> dict:
    config = {
        "schemaVersion": 2,
        "deviceId": device,
        "asideRoot": str(root),
        "agentId": "main",
        "asideSyncPath": str(SYNC),
        "syncDir": str(sync_dir),
        "intervalSeconds": 300,
        "exportStatuses": ["idle", "interrupted", "errored"],
        "scope": {"mode": "all", "afterDate": None, "sessionIds": [], "excludeStatuses": []},
        "files": {"artifacts": True, "attachments": False},
        "security": {
            "encryption": encryption, "encryptIndex": False, "titlesInIndex": True,
            "keyFile": str(path.parent / "age.key"), "keychainService": "aside-sync-tests",
        },
        "locks": {"leaseSeconds": 900, "graceSeconds": 120},
        "conflictPolicy": policy,
        "logPath": str(path.parent / f"{device}.log"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def append_message(root: Path, sid: str, marker: str) -> str:
    sdir = session_directory(root, sid)
    if sdir is None:
        raise FileNotFoundError(f"session directory not found: {sid}")
    with (sdir / "messages.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "user", "content": marker}) + "\n")
    con = sqlite3.connect(root / "state.db")
    con.execute("UPDATE sessions SET updated_at=? WHERE id=?", (int(time.time()), sid))
    con.commit()
    con.close()
    return sha256(sdir / "messages.jsonl")
