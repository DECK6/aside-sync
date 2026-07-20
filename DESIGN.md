# aside-sync v2 — Product Design

Headless, transport-agnostic handoff sync for Aside sessions across devices
(Obsidian-Sync-like UX, shared-folder based). This document is the
authoritative spec for the v2 implementation. The current MVP
(`bin/aside-sync`, `bin/aside-syncd`, copied from `~/.aside/tools/`) is the
baseline to be refactored — it works but is NOT production-safe.

Treat this as a **sensitive local-data sync tool**. Security, explicit user
choice, reversibility, backups, and conflict safety are first-class
requirements, not polish.

---

## 0. Context (verified on this machine)

- Aside user root: `~/.aside/u/0`
  - `state.db` (SQLite, WAL mode — `-wal`/`-shm` present)
  - `credentials.json`, `passwords/`, `settings.json`, `models.json`,
    `suggested-items/` — **must never enter a bundle or the sync dir**
  - sessions: `~/.aside/u/0/agents/<agentId>/sessions/<date>_<sessionId>/`
    containing `messages.jsonl`, `artifacts/`, `tmp/`, sometimes `attachments/`
- `state.db` tables: `agents, channel_connections, channel_pairing_requests,
  inbox_messages, notification_grants, routines, sessions, session_runs,
  session_tabs`. Only the last three are sync-relevant.
  `channel_connections` holds live bot tokens — this is why shipping the whole
  DB in a bundle (MVP v1 behavior) is a real credential leak.

### Verified schema (2026-07-20)

`sessions` columns:
`id, agent_id, parent_id, branched_from, title, trigger,
trigger_idempotency_key, routine_id, channel_route_key, status, system_prompt,
model, permission_mode, permission, context_window, queued_messages,
steering_messages, cwd, suspension, tool_state, active_tab_target_id,
incognito, ephemeral, runtime_config, read_at,
latest_compaction_message_offset, archived_at, created_at, updated_at,
browser_binding`

`session_runs` columns:
`id (autoincrement), session_id, user_message, final_assistant_message,
files_changed, token_usage, started_at, last_message_timestamp, finished_at,
aborted_at, abort_reason, jsonl_read_offset, jsonl_read_size`
- UNIQUE index on `(session_id, jsonl_read_offset)`.

`session_tabs` columns:
`id (TEXT PK), session_id, ownership, source, target_id, url, data,
created_at, updated_at`
- UNIQUE index on `(session_id, target_id)`.
- `session_tabs` rows are REQUIRED for message attachment rendering
  (learned the hard way — missing rows break message loading). Preserve
  `target_id` verbatim; regenerate `id` only on collision.

### Known UI landmines (from manual import experiments, must stay fixed)

1. `tool_state = "{}"` crashes the UI (`Cannot read properties of undefined
   (reading 'todos')`). Import must always write the minimal tool_state:
   ```json
   {"todo":{"todos":[]},"execution":{"totalRunMs":0},
    "bash":{"cwd":"<local session dir>"},"skills":{},"question":{},
    "subagent":{"subagents":[]}}
   ```
2. Sidebar open path is sensitive to live-binding fields. Import must force:
   `active_tab_target_id=NULL, browser_binding=NULL,
   latest_compaction_message_offset=0, status='idle',
   queued_messages='[]', suspension=NULL`.
3. Missing `session_tabs` rows → session opens from full list but messages
   fail to load. Always import them.

---

## 1. Architecture

Two tools, one direction of dependency:

- **`aside-sync`** — core engine. Bundle format, export/import, sanitization,
  validation, encryption, SSH convenience transport. No daemon logic, no
  config file dependency (all via flags; config-aware defaults are fine).
- **`aside-syncd`** — orchestration. Setup wizard, config, shared-folder
  loop, locks, conflicts, index, LaunchAgent lifecycle. Invokes `aside-sync`
  as a subprocess (keeps process isolation; do not import it as a module).

Constraints:
- Python 3 stdlib only. `pathlib` everywhere. macOS first; nothing
  macOS-only in the core engine (launchd/Keychain code isolated in clearly
  marked functions).
- No Tailscale assumptions. SSH is an optional convenience transport only;
  the shared-folder mode must work with zero network tooling.
- Never `rm`/`unlink` user-valuable data. Cleanup = move to `~/.Trash/`
  with a unique name. (Our own staging tmp files inside a `.staging/` dir we
  created may be os.replace'd/overwritten freely; genuinely disposable
  in-process tempdirs via `tempfile` are fine.)
- Repo layout:
  ```
  aside-sync/
  ├── DESIGN.md                 (this file)
  ├── README.md                 (user docs + security model + limitations)
  ├── bin/aside-sync            (single-file python, executable)
  ├── bin/aside-syncd           (single-file python, executable)
  ├── install.sh                (copy bin/* → ~/.aside/tools/, backup old to Trash)
  └── tests/                    (unittest, no external deps)
  ```

---

## 2. Bundle format v2 (security core)

A bundle is `tar.gz`, optionally wrapped in encryption (§4). **No SQLite DB
inside. Ever.**

```
<sessionId>__<deviceId>-<msgHash16>.tgz[.age|.gpg|.enc]
├── manifest.json
├── db.json
└── files/
    ├── messages.jsonl
    ├── artifacts/**          (default on)
    └── attachments/**        (default OFF, config opt-in)
```
`tmp/` is never included (no config option to include it).

### manifest.json
```json
{
  "format": "aside-session-bundle",
  "schemaVersion": 2,
  "sessionId": "...", "agentId": "main", "sessionDirName": "2026-07-20_xxx",
  "sourceDeviceId": "macbookair2",
  "exportedAt": 1789000000,
  "title": "...", "status": "interrupted",
  "createdAt": 0, "updatedAt": 0,
  "messages": {"bytes": 0, "sha256": "...", "lines": 735},
  "files": [{"path": "files/artifacts/a.png", "bytes": 0, "sha256": "..."}],
  "includes": {"artifacts": true, "attachments": false},
  "dbCounts": {"runs": 11, "tabs": 1}
}
```

### db.json — whitelist export, not blacklist

`sessions` row: export ONLY these fields (`EXPORT_SESSION_FIELDS`):
`id, agent_id, parent_id, branched_from, title, status, system_prompt, model,
permission_mode, permission, context_window, cwd, incognito, created_at,
updated_at`

Everything else is deliberately absent — including `tool_state`,
`browser_binding`, `active_tab_target_id`, `queued_messages`,
`steering_messages`, `suspension`, `runtime_config`, `trigger`,
`trigger_idempotency_key`, `routine_id`, `channel_route_key`, `read_at`,
`latest_compaction_message_offset`, `ephemeral`, `archived_at`.
Rationale: whitelisting means future schema columns can't silently leak.
`channel_route_key`/`routine_id` are excluded so an imported session can never
hijack a channel route or routine on the target device.

`session_runs`: all columns except `id`. `session_tabs`: all columns as-is.

### Import rules

- Verify `schemaVersion == 2` (refuse >2 with "upgrade aside-sync" message;
  refuse v1 outright — see §8 scrub).
- Verify sha256 of `messages.jsonl` and every listed file before touching
  anything. Verify every `messages.jsonl` line parses as JSON.
- Validate local DB has the required tables/columns
  (`PRAGMA table_info`); bundle fields missing from local schema are ignored
  with a warning (forward compat).
- Forced local values on insert (`IMPORT_FORCED_FIELDS`) — see §0 landmines,
  plus: `cwd = <dest session dir>`, `read_at = now`, `ephemeral = 0`,
  `archived_at = NULL`. Columns not provided rely on schema defaults.
- FK safety: `agent_id` must exist in local `agents` (else fail with clear
  message); `branched_from` nulled if referent absent locally.
- `session_runs`: insert without `id`; the UNIQUE `(session_id,
  jsonl_read_offset)` index is satisfied because update-existing deletes rows
  first.
- Refuse import when the local session `status == 'running'` (never overwrite
  a live session). Use `busy_timeout=5000` and `BEGIN IMMEDIATE`; if the DB
  is locked, abort cleanly with a retryable error.
- `sqlite3 .backup` of local `state.db` to `~/.aside/backups/...` before any
  mutation (keep newest 20 backup dirs; move older to Trash).
- Session folder update: back up existing dir into the backup dir, then copy.
  Existing size-regression guard (`--force` to override) stays.
- `--as-new-session` (fork import, used by conflict handling §6):
  generate a fresh 16-char alnum session id, new dir
  `<today>_<newSid>`, rewrite `session_id` in runs/tabs, `branched_from` =
  original sid if it exists locally, title += ` (conflict from <srcDevice>)`
  (suffix overridable via `--title-suffix`).
  Open question for implementation: check whether `messages.jsonl` embeds the
  session id in metadata lines (inspect a real session first). If it does and
  the UI keys on it, rewrite those references or document the limitation and
  verify rendering in QA.
- `--dry-run` on import/push/pull: print the full planned action set as JSON
  (rows to insert, files to write, backups to create), touch nothing.
- New `inspect-bundle` command: print manifest (+ decrypt if needed) without
  importing.

### Atomicity

- Export: build in `tempfile` dir → write final artifact as
  `<dest>/.staging/<name>` → `os.replace` into place (same filesystem as
  dest, safe for cloud-synced folders that upload partial files).
- Index/state/lock JSON writes: same-dir `.tmp` + `os.replace` (existing
  `save_json` pattern, keep).

---

## 3. Config v2 (`~/.aside/sync/config.json`)

```json
{
  "schemaVersion": 2,
  "deviceId": "macstudio",
  "asideRoot": "~/.aside/u/0",
  "agentId": "main",
  "asideSyncPath": "~/.aside/tools/aside-sync",
  "syncDir": "/path/to/shared/AsideSync",
  "intervalSeconds": 300,
  "exportStatuses": ["idle", "interrupted", "errored"],
  "scope": {"mode": "all", "afterDate": null, "sessionIds": [],
            "excludeStatuses": []},
  "files": {"artifacts": true, "attachments": false},
  "security": {"encryption": "none", "encryptIndex": false,
               "titlesInIndex": true, "keyFile": "~/.aside/sync/age.key",
               "keychainService": "aside-syncd"},
  "locks": {"leaseSeconds": 900, "graceSeconds": 120},
  "conflictPolicy": "record",
  "logPath": "~/.aside/logs/aside-syncd.log"
}
```

- Loading a v1 config (no `schemaVersion`): migrate in place, back up the old
  file beside it as `config.json.v1.bak`.
- `scope.mode`: `all` | `after` (with `afterDate`) | `selected` (with
  `sessionIds`). Archived and ephemeral sessions are never exported.
  `excludeStatuses` lets the user drop `errored`/`interrupted` from export.

---

## 4. Encryption (optional, required before cloud folders per README)

Pluggable backends, all invoked as subprocesses, whole-bundle encryption
(manifest included → titles/content never plaintext in the sync dir):

| backend | mechanism | notes |
|---|---|---|
| `age` (preferred) | X25519 identity file, `age -e -r <recipient>` / `age -d -i <keyfile>` | wizard runs `age-keygen` if `age` installed; keyfile `~/.aside/sync/age.key`, chmod 600 |
| `gpg` | symmetric `gpg -c --batch --passphrase-fd 0` | passphrase stored in macOS Keychain (`security add-generic-password` / `find-generic-password -w`), service = `keychainService` |
| `openssl` (fallback) | `openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -pass fd:...` | NOT authenticated encryption; integrity comes from inner sha256 manifest. README must state this caveat plainly |
| `none` | — | wizard shows a strong warning when syncDir looks like a cloud folder |

Rules:
- The key/passphrase **never** enters `syncDir`. Wizard prints explicit
  instructions: copy `age.key` between devices via AirDrop/USB, never via the
  sync folder.
- Encrypted artifacts keep suffixes: `.tgz.age` / `.tgz.gpg` / `.tgz.enc`.
- `encryptIndex: true` → `indexes/<device>.json.age` etc. Locks and conflict
  records stay plaintext for coordination but contain only ids, hashes,
  device ids, timestamps — never titles (`titlesInIndex` only affects the
  index; with `encryptIndex` the point is moot).
- Decryption failures are hard errors (no silent fallback to plaintext
  parsing).

---

## 5. Sync dir layout + lock/ownership protocol

```
syncDir/
├── bundles/<sessionId>/<deviceId>-<msgHash16>.tgz[.enc...]
├── indexes/<deviceId>.json[.age...]        (device inventory + heartbeat)
├── locks/<sessionId>.json                  (plaintext, low-sensitivity)
├── conflicts/<sessionId>-<ts>.json
└── .staging/                               (atomic write workspace)
```

### Lock file
```json
{"schemaVersion": 1, "sessionId": "...", "ownerDeviceId": "...",
 "acquiredAt": 0, "leaseUntil": 0, "updatedAt": 0, "sessionHash": "sha256"}
```

Semantics (advisory — shared-folder replication is eventually consistent;
conflict detection §6 remains the backstop):

- Before exporting an update for session S: read lock. Export only if lock is
  absent, owned by self, or expired (`leaseUntil < now - graceSeconds`).
  Acquire/renew by atomically writing the lock with
  `leaseUntil = now + leaseSeconds`.
- If another device holds a valid lease → skip exporting S this cycle (log
  it), and prefer importing that device's updates.
- Release = rewrite the lock with `leaseUntil: 0` (tombstone; never delete
  files in the shared folder).
- Manual commands: `aside-syncd claim SESSION_ID` (force-acquire, logged as
  takeover), `aside-syncd release SESSION_ID`.
- Clock skew: tolerate via `graceSeconds`; document the assumption that
  devices are NTP-synced within ~2 min.

---

## 6. Conflict handling

Conflict = local messages.jsonl diverged from the last imported hash AND the
remote index advertises a newer/different hash for the same session.

- **Never** auto-overwrite either side.
- Always write `conflicts/<sessionId>-<ts>.json`:
  `{sessionId, localDeviceId, sourceDeviceId, localHash, remoteHash,
  bundlePath, detectedAt, resolution: null}`.
- `conflictPolicy`:
  - `record` (default): record + skip; surfaced in `aside-syncd status`.
  - `fork`: additionally import the remote bundle via `--as-new-session`
    (title suffix ` (conflict from <device>)`), mark
    `resolution: "forked:<newSid>"`.
- Manual resolution: `aside-syncd resolve SESSION_ID --take local|remote|fork`
  - `local`: mark resolved, force-export local as the new authoritative
    version (claims lock).
  - `remote`: back up local session (DB rows + dir → backups), then import
    remote with `--update-existing --force`.
  - `fork`: as above.

---

## 7. Daemon & scheduling

Cycle (`run-once`): load config → ensure syncDir skeleton → renew/acquire
locks for owned changed sessions → export changed (scope-filtered,
status-filtered, lock-gated) → write index atomically (heartbeat) → import
remote (lock-aware, conflict policy) → persist state
(`~/.aside/sync/state.json`) → structured log line.

- `daemon`: run-once loop with `intervalSeconds` sleep (non-launchd
  platforms / foreground debugging).
- **LaunchAgent mode (production on macOS): `StartInterval` + `run-once`**,
  not KeepAlive + internal loop — self-healing, no long-lived process.
  Plist generated from config (label `com.deck.aside-syncd`, `ProcessType:
  Background`, stdout/err → `logPath`).
- `install-launchagent`: write plist → `launchctl bootstrap gui/$UID <plist>`
  (fallback to `load` if bootstrap unsupported) → `launchctl enable`.
  Never installed by setup wizard without explicit user confirmation.
- `uninstall-launchagent`: `launchctl bootout` → move plist to Trash.
- `disable`: bootout only (config/data untouched).
- `uninstall`: bootout, move plist + config + state to Trash; print what
  intentionally remains (syncDir contents, backups, key file — never touch
  the key file automatically).
- Logs: JSON lines at `logPath`; rotate at 5 MB (rename to `.1`, keep 3).
- `run-once --dry-run`: full cycle simulation, JSON report, no writes.

---

## 8. Migration & remediation of v1 damage

- `import-bundle` refuses v1 bundles (they contain a full `state.db`).
- New command `aside-sync scrub-v1 <syncDir>`: find v1 bundles
  (`manifest.format == aside-session-bundle-v1` or `source-state.db` member),
  move them to `~/.Trash/`, report count. README warns: cloud providers keep
  version history — rotate any credentials that were in a synced `state.db`
  (`channel_connections` bot tokens) if the folder was cloud-hosted.
- `aside-syncd` state v1 → v2 migration on load (per-session structure kept).

---

## 9. Setup wizard (`aside-syncd setup`)

Interactive, stdlib `input()`, numbered choices, sensible defaults shown.
Steps:

1. **Sync folder**: auto-detect and offer iCloud Drive
   (`~/Library/Mobile Documents/com~apple~CloudDocs`), Dropbox
   (`~/Library/CloudStorage/Dropbox*`, `~/Dropbox`), Google Drive
   (`~/Library/CloudStorage/GoogleDrive-*/My Drive`), Syncthing/custom path.
   Default subfolder `<chosen>/AsideSync`. Validate writability.
2. **Device id**: default = sanitized hostname; must be unique per device
   (README explains why).
3. **Sync scope**: all / after date / selected session ids;
   optionally exclude `errored`/`interrupted`.
4. **File scope**: messages only / +artifacts (default) / +attachments.
   State clearly that `tmp/` is always excluded.
5. **Security**: none (trusted local folder only — print strong warning if
   the chosen folder is under a known cloud path) / encrypt bundles /
   encrypt bundles + index. Backend auto-pick: `age` if installed, else
   `gpg`, else `openssl` (with caveat shown). Key generation + cross-device
   key copy instructions printed.
6. **Automation**: manual only / foreground daemon / LaunchAgent.
   **Wizard never starts anything.** It ends by printing the exact commands:
   `aside-syncd run-once` (try it), `aside-syncd install-launchagent`
   (enable), `aside-syncd uninstall` (undo everything).
7. **Summary + confirm** before writing `~/.aside/sync/config.json`.

`init` stays as the non-interactive advanced path (flags for everything, used
by tests).

---

## 10. Non-goals (v2)

- No message-level merge of diverged `messages.jsonl` (append-only
  assumption; divergence → conflict/fork).
- No Windows/Linux service integration (daemon mode works there; launchd
  only on macOS).
- No multi-user roots (`u/0` only), single `agentId` per config.
- No relay server; shared folder or SSH only.

## 11. Security model summary (goes into README)

- Bundle contains ONLY: 3 whitelisted-table row sets + messages.jsonl +
  chosen file dirs. Never state.db, never credentials/passwords/settings,
  never other tables, never tmp/.
- Whitelist export → unknown future columns can't leak.
- Live-binding/channel/routine fields never leave the device.
- Optional whole-bundle encryption; key never in the sync folder.
- Every import: hash-verified, schema-validated, transactional, preceded by
  a state.db backup and session-dir backup; running sessions untouchable.
- Everything reversible: backups + Trash, no hard deletes.
- Residual risks documented: cloud version history, openssl-CBC
  non-AEAD fallback, advisory locks, same-user local attacker out of scope.

## 12. Acceptance criteria (from product owner, verbatim contract)

- `aside-syncd setup` covers folder/device/scope/security/automation.
- Headless sync without SSH works.
- Shared folder contains no full state.db (and `scrub-v1` cleans old ones).
- Bundles contain only necessary session data.
- Imported sessions open from BOTH full session list and sidebar.
- Running sessions never overwritten.
- Conflicts recorded, never silently overwritten.
- Optional encryption available before using cloud folders.
- Clear uninstall/disable commands.
- README documents security model and limitations.

## 13. Test plan (unittest, stdlib only)

`tests/` with a fixture builder that creates a synthetic aside root
(state.db from the schema in §0, fake sessions with messages.jsonl +
artifacts + tabs). No dependency on the real `~/.aside`.

- `test_bundle.py`: export → member list exactly {manifest.json, db.json,
  files/*}; no `.db` member; whitelist enforced (assert excluded fields
  absent); hashes correct; jsonl validation catches a corrupt line; atomic
  staging (no partial file at final path on failure).
- `test_import.py`: fresh import → forced fields correct (tool_state,
  status idle, bindings null); update-existing → backup created, rows
  replaced; running-session refusal; v1 bundle refusal; schema-mismatch
  warning path; fork import → new sid everywhere, branched_from set,
  title suffix; dry-run touches nothing (fs + db snapshot compare).
- `test_locks.py`: acquire/renew/expire/takeover/release-tombstone; skip
  export when foreign lease valid.
- `test_conflicts.py`: diverged-hash detection, record file shape, fork
  policy end-to-end, resolve --take local|remote.
- `test_daemon.py`: two synthetic roots + one syncDir → device A exports,
  device B imports; second cycle no-op (hash dedupe); index heartbeat;
  encryption round-trip per backend (skip if binary missing).
- `test_wizard.py`: scripted stdin through setup; config written matches
  choices; no daemon started.
- E2E happy path script reused in CI-style run: `python3 -m unittest
  discover -s tests -v`.

## 14. Implementation order

1. **Phase 1 — bundle v2 + import hardening** (closes the active security
   hole): export whitelist, db.json, validation, atomicity, dry-run,
   inspect-bundle, fork import, v1 refusal + scrub-v1, tests.
2. **Phase 2 — daemon hardening**: config v2 + migration, locks, conflicts
   (record/fork/resolve), atomic index, encryption backends, log rotation,
   tests.
3. **Phase 3 — UX & lifecycle**: setup wizard, LaunchAgent
   bootstrap/bootout, disable/uninstall, README (security model,
   limitations, key-copy instructions), install.sh.

Each phase lands with its tests green before the next starts.
