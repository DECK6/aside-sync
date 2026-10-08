# aside-sync v2 — macOS and Windows

`aside-sync` securely packages an Aside session, and `aside-syncd` coordinates
those packages through an ordinary shared folder (iCloud Drive, Dropbox,
Google Drive, Syncthing, NAS, or an external disk). SSH `push`/`pull` remains an
optional convenience; headless shared-folder sync needs no network-specific
tool.

## Prerequisites: how devices exchange files

aside-sync does no cloud authentication of its own. It reads and writes an
ordinary local folder; whatever replicates that folder carries the bundles.
Pick whichever rung you already have:

1. **Same cloud account on both devices** (typical two-Mac case: iCloud Drive
   is already on) — the wizard auto-detects the folder; zero extra setup.
   Google Drive detection covers localized folder names (e.g. `내 드라이브`).
2. **No shared cloud account**: use a cloud "shared folder" invited to both
   accounts (enable encryption), or Syncthing (no account, P2P), or a
   NAS/SMB/USB path entered as a custom folder.
3. **Nothing shared at all**: manual handoff still works —
   `aside-sync export-bundle` → AirDrop/USB → `aside-sync import-bundle`.

Note for SSH administration: macOS TCC blocks remote (sshd) sessions from
reading iCloud Drive and Documents unless "Allow full disk access for remote
users" is enabled under System Settings → General → Sharing → Remote Login.
This affects only commands run over SSH; the daemon running locally on the
device is unaffected.

Note for LaunchAgent mode with an iCloud (or otherwise TCC-protected) sync
folder: the launchd-spawned python has no interactive TCC context, so macOS
may silently deny it ("Operation not permitted") or fail dataless-file reads
with EDEADLK. Grant Full Disk Access to the exact python binary recorded in
the generated plist (System Settings → Privacy & Security → Full Disk Access
→ +). Trade-off: every script run by that interpreter then has full-disk
read access. The daemon requests dataless downloads via `brctl download` and
skips still-unreadable files until the next cycle instead of crashing.
On **each Mac**, also select the shared `AsideSync` folder in Finder, right-click
it and enable **Keep Downloaded** (`다운로드 항목 유지`). The setting is local to
that computer. Otherwise indexes can arrive while bundles remain online-only,
and a cycle can exit successfully while reporting `import-failed`/`EDEADLK`.
Check imported counts and skipped reasons as well as the scheduler exit code.

## Install and set up

Run `./install.sh` to copy the two executables to `~/.aside/tools/`. Existing
copies are moved to `~/.Trash` with unique names first. Then run:

```sh
~/.aside/tools/aside-syncd setup
~/.aside/tools/aside-syncd run-once --dry-run
~/.aside/tools/aside-syncd run-once
```

The wizard chooses the folder, unique device id, session and file scope,
security, and automation instructions. It never starts a daemon or installs a
LaunchAgent. Device ids must be different on every device because they identify
bundle authors, index ownership, and lock owners.

Advanced non-interactive setup is available with `aside-syncd init --help`.
The default configuration is `~/.aside/sync/config.json`; daemon state is
`~/.aside/sync/state.json`.

### Windows

Install Python 3.9+ and open Aside once so its local database exists. From this
repository in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" setup
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" run-once --dry-run
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" run-once
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" install-scheduled-task
```

The installer creates `.cmd` launchers pinned to the discovered Python and
recycles previous tools. No administrator account or task password is needed.
The task runs while that Windows user is logged in, including on battery, and
skips overlapping scheduled runs. Keep Python installed at that path; rerun the
installer and task registration after changing Python installations.

On every computer, choose **the same replicated folder** and a **different
device id**. Windows setup detects iCloud Drive, OneDrive (including business
accounts), Dropbox, Google Drive virtual drives and Syncthing. Custom paths
work for other mount locations. Set cloud files to remain available locally.
Installing this tool does not install or authenticate a cloud client.

Windows data/config defaults are `%USERPROFILE%\.aside\u\0` and
`%USERPROFILE%\.aside\sync`. For a different Aside root, use
`aside-syncd init --aside-root PATH --sync-dir SHARED_PATH --device-id UNIQUE_ID`.
Do not copy a Mac config verbatim: its paths and device id belong to that Mac.

```powershell
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" disable
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" uninstall-scheduled-task
& "$env:USERPROFILE\.aside\tools\aside-syncd.cmd" uninstall
```

`disable` retains the task/config/data. Task removal saves its XML locally;
`uninstall` also recycles config/state and retains shared bundles, backups and
encryption keys. `--dry-run` is available on these commands and task installation.
The Windows Recycle Bin owns recycled file locations; receipts identify source
paths. A custom `--trash-dir` remains available for import backup retention.

For scheduled macOS sync:

```sh
aside-syncd install-launchagent
aside-syncd disable                 # stop scheduling; retain config/data
aside-syncd uninstall-launchagent  # stop and Trash only the plist
aside-syncd uninstall              # stop; Trash plist, config, and state
```

The LaunchAgent uses `StartInterval` to execute one self-contained `run-once`;
it is not a long-lived KeepAlive process. `uninstall` deliberately retains the
shared folder, backups, and encryption key.

## Encryption and copying keys

Encryption is strongly recommended before putting bundles in a cloud-hosted
folder. It covers the whole bundle, including its manifest, and can separately
cover device indexes. Locks and conflict records remain plaintext but contain
only ids, hashes, device ids, paths, and timestamps—never titles or messages.

- `age` is preferred. The default identity is
  `~/.aside/sync/age.key` with mode 0600. Copy that file to the identical path
  on every device using AirDrop or a USB drive. **Never put the key in the sync
  folder.**
- `gpg` and `openssl` use a symmetric passphrase stored under the configured
  `keychainService` in macOS Keychain. The setup wizard asks you to type the
  passphrase (or generates a random one if you leave it blank): run setup on
  every device and **enter the same passphrase** — no key files or manual
  Keychain commands needed.
- `openssl` is the fallback (`AES-256-CBC`, PBKDF2, 600,000 iterations). CBC is
  not authenticated encryption; the verified hashes inside the decrypted
  manifest provide integrity detection.
- `none` is appropriate only for a trusted local/non-cloud folder.

Encryption can be toggled later without redoing setup: `aside-syncd encryption
off`, `aside-syncd encryption on [--index]`, `aside-syncd encryption status`.
Turning it off only affects newly exported bundles — already-synced encrypted
bundles still need the key to import.

For unattended symmetric operation outside the macOS Keychain integration,
set `ASIDE_SYNC_PASSPHRASE` in the process environment. Passphrases are never
placed in command-line arguments or written to the sync directory.
On Windows, install `age` and `age-keygen` for key-file encryption, or arrange
for that environment variable to be present in both setup and the scheduled
process. The wizard does not try to use macOS Keychain on Windows.

## Core commands

```sh
aside-sync export-bundle SESSION_ID --output /path/bundle.tgz --source-device-id DEVICE
aside-sync inspect-bundle /path/bundle.tgz
aside-sync import-bundle /path/bundle.tgz
aside-sync import-bundle /path/bundle.tgz --update-existing
aside-sync import-bundle /path/bundle.tgz --as-new-session
aside-sync scrub-v1 /path/to/AsideSync --dry-run
aside-sync scrub-v1 /path/to/AsideSync

aside-syncd status
aside-syncd status --check
aside-syncd claim SESSION_ID
aside-syncd release SESSION_ID
aside-syncd resolve SESSION_ID --take local   # or remote/fork
```

Import, SSH push/pull, daemon cycles, and v1 scrubbing support `--dry-run` where
applicable. A daemon conflict is always recorded and never silently overwrites
either side. The default policy records and stops; `fork` also imports the
remote history under a new session id.

### Checking whether sync is actually working

`status` reports the last cycle's time, export/import counts, skipped reasons,
unreadable indexes/conflict records, peer heartbeats and unresolved conflicts. `status --check`
prints the same JSON and exits **1** when attention is needed; plain `status`
still exits **0** when the report can be read. A sleeping peer, a pending lock,
or an unresolved conflict also counts as attention, not necessarily a failure.
A cycle or peer is stale after three configured intervals (at least 15 minutes).
After upgrading, run or wait for one cycle to record the new summary.

An iCloud `Resource deadlock avoided` error is reported with **Keep Downloaded**
guidance. Windows users should mark cloud folders **Always keep on this device**.
A missing bundle can also mean cloud replication has not finished yet.
The check reports available evidence; it cannot prove that every peer has
received every conversation. Fatal cycle failures remain errors; individual
session skips allow other sessions to finish and remain visible in the summary.
An inaccessible shared folder is reported separately from a missing peer.
On macOS, an SSH status command may lack iCloud access while the local scheduled
cycle succeeds; read `lastCycle` and check status in Terminal on that Mac.

### Relationship to Aside's built-in Sync

Reviewed against Aside components **1.26.1007.1853** (October 8, 2026):

- Account Sync handles browser data and recovery; Password Manager has its own
  sync/recovery controls.
- Experimental Sync covers agent files, skills, selected memory, project
  metadata, routines, browsing history and cookies. Its reviewed transfer
  collections do **not** include conversation transcripts or session attachments.
- This tool supplies session history and selected session files through your
  shared folder. It does not change Aside's built-in sync settings.

`status` includes `asideExperimentalSync.enabled` from the configured Aside
root (`null` means unavailable/unknown). This is informational and does not
affect the health check. Installing this tool neither enables nor disables
Aside's cloud features. Recheck the scope when future Aside versions change it.

## Security model

A v2 bundle contains only:

- one `sessions` row exported through an exact 14-field base whitelist, plus
  optional `agent_id`, `harness_id` and `transcript_revision` when present;
- whitelisted history from `session_turns` (October 2026) or legacy
  `session_runs`, without its local autoincrement id;
- required `session_tabs`, preserving attachment `target_id` values;
- `messages.jsonl`; and selected `artifacts/` and `attachments/` files.

It never contains `state.db`, credentials, passwords, settings, models, other
database tables, `tmp/`, tool state, browser binding, active-tab binding,
channel/routine routing, queued/steering messages, suspension, runtime config,
or future unknown session columns. Because export is a whitelist, a new local
schema column cannot leak automatically.

Every import validates bundle version, member paths/types, exact member list,
byte counts, SHA-256 hashes, and every JSONL line before touching local state.
It validates the required local schema and, on legacy agent-scoped databases,
validates agent ownership. Current agentless Aside databases and legacy
`agents/<agentId>/sessions` layouts are both supported. It refuses a running
local session, obtains a SQLite immediate write lease, and mutates rows
transactionally. Before mutation it uses SQLite's backup API and moves the old
session directory into the same import backup. The newest 20 import backups are
kept; older backups go to Trash. Imported live bindings are nulled and a
sidebar-safe minimal `tool_state` is generated locally.

Exports, indexes, state, and locks publish through a same-filesystem temporary
path plus `os.replace`, so cloud replication never sees a partially written
artifact. Shared-folder locks are advisory leases, with hash-based conflict
detection as the backstop. Keep device clocks NTP-synchronized within roughly
two minutes (the default grace window).

The tools move user-valued cleanup targets to Trash rather than hard-deleting
them. Decryption failures are hard errors and never fall back to plaintext.

## v1 credential remediation

v1 bundles are refused because they included a full `state.db`, potentially
including `channel_connections` bot tokens. Preview and remove them with
`aside-sync scrub-v1 SYNC_DIR --dry-run`, then run without `--dry-run`; matching
bundles are moved to Trash. Cloud providers may retain old file versions. If v1
bundles ever entered a cloud-hosted folder, purge provider version history and
rotate every credential that could have been stored in `state.db`.

## Limitations and non-goals

- There is no message-level merge. Divergence becomes an explicit conflict or
  fork.
- Fork import is content-safe because Aside `messages.jsonl` does not embed the
  session id. Runs and tabs receive the new session id, while tab `target_id`
  values remain verbatim. Tab row ids are regenerated only on collision.
- The engine assumes session histories are append-only for change and conflict
  detection.
- Locks are advisory because shared folders are eventually consistent. Hash
  validation and conflict records remain authoritative.
- Service integration uses macOS launchd or Windows Task Scheduler. Foreground
  `daemon` mode works on other platforms. Avoid starting manual sync cycles
  while a scheduled cycle is running.
- Only Aside user root `u/0` and one configured agent id are supported.
- There is no relay server, Tailscale dependency, or multi-user-root support.
- A malicious process already running as the same local user is outside the
  threat model.
- Cloud version history and provider-side plaintext metadata remain residual
  risks; use whole-bundle and index encryption and rotate exposed v1 secrets.

## October 2026 compatibility

The old daemon failed with `near "FROM": syntax error` after Aside renamed
`session_runs` to `session_turns`. The engine now detects the local table and
maps it to the existing v2 wire key `runs`, preserving known turn metadata.
Unknown future history columns are excluded. Transcript revisions advance when
replacing an existing session, and a failed export is reported and retried
without stopping the rest of the cycle. Conflicts still require explicit
resolution; no side is silently selected.

The regression suite covers all nine source/destination combinations of the
legacy agent schema, agentless runs schema and October turns schema. Windows
process/task behavior has automated tests; native Windows end-to-end execution
must be checked on a Windows host. Current upstream references:
[Aside component changelog](https://docs.aside.com/changelog/components) and
[Microsoft Task Scheduler](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).
