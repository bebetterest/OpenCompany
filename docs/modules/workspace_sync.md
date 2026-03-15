# Workspace Sync Module

## Scope

Workspace and project sync behavior is implemented by:

- `opencompany/workspace.py`
- project-sync helpers in `opencompany/orchestrator.py`

## Workspace Modes

Every session persists one workspace mode:

- `direct` (default for new sessions): root and worker agents share the live target project directory. Changes take effect immediately.
- `staged`: edits happen inside session workspaces first, and root finalization stages a project diff for later confirmation.

The selected mode is chosen only when creating a new session. Reconfigure/load/resume keep the original mode locked.

## Remote Direct Workspace (SSH, V1)

- Remote workspace is supported only in `direct` mode.
- `staged` + remote is blocked in both UI setup and backend validation.
- Remote target format is `user@host[:port]`; remote workspace path must be an absolute Linux path.
- Session-level remote metadata is persisted at `.opencompany/sessions/<session_id>/remote_session.json`.
  - stored fields: `ssh_target`, `remote_dir`, `auth_mode`, `identity_file`, `known_hosts_policy`, `remote_os`
  - password is not written to `remote_session.json` (plaintext is never stored there)
- Remote config is loaded once when importing session context and reused in-memory for runtime/terminal paths.

## Workspace Topology

Per session directory:

- `snapshots/`: staged-mode baseline snapshots (`root_base` and child bases)
- `workspaces/`: writable child workspaces
- `diffs/`: per-agent diff artifacts (`<agent_id>.json`)

Root workspace creation in `staged` mode:

- copy project into `snapshots/root_base`
- clone into `snapshots/root` as root working view

Root workspace creation in `direct` mode:

- use the configured live target directory as `root.path` (local path or remote path mapped from remote config)
- set `root.base_snapshot_path` to the same live target directory
- skip `snapshots/root_base` and `snapshots/root`

Child workspace creation:

- `staged`: fork from parent workspace snapshot into isolated `workspaces/<agent_id>`
- `direct`: workers reuse `workspace_id="root"` and write into the live target project directory

## Change Tracking

`WorkspaceManager` computes:

- `added`, `modified`, `deleted`
- text/binary-aware file diff previews
- diff artifact JSON containing file lists, patches, and patch metadata

Ignored paths include runtime/internal noise (`.git`, `.opencompany`, caches, `.env*`, etc.).
Symlinks are excluded end-to-end (snapshot, tree traversal, search, diff, and apply/undo sync paths).

## Upward Promotion

When a worker completes (status `completed` or `partial`) in `staged` mode:

1. worker delta is applied onto parent workspace
2. diagnostics record counts of promoted paths
3. worker completion still records its own diff artifact

If promotion fails:

- worker completion is downgraded to `failed`
- summary/recommendation are adjusted for recovery guidance

In `direct` mode there is no upward promotion step because worker edits are already live in the shared root workspace. Worker diff artifacts are also skipped because per-worker diffs are no longer reliable in a shared-write session.

## Stage / Apply / Undo Model

`staged` mode root finalization stages project sync state first:

- state file: `project_sync.json`
- backups directory: `project_sync_backups/`
- status transitions: typically `pending -> applied -> reverted`

`apply`:

- copies staged changes to target project
- writes backup manifest for rollback

`undo`:

- restores files from backup metadata
- removes applied additions when possible

`direct` mode disables project sync entirely:

- project-sync status is reported as `disabled`
- diff preview/apply/undo APIs fail fast
- Web UI and TUI disable `Diff` / `Apply` / `Undo`

## Remote Runtime Cleanup

For remote `direct` sessions:

- per command: delete one-time local password temp files (password auth path)
- session end/interruption/failure: close local SSH control socket state
- remote cache GC: remove transient `exec_*.sh`, `*.lock`, `*.pid`, while keeping reusable settings artifacts for resume

## Operational Contract

- `staged` sessions can complete with staged changes un-applied.
- `staged` final writeback requires explicit user confirmation (`apply`).
- `undo` is scoped to the recorded last apply for that `staged` session.
- `direct` sessions have no runtime rollback layer; users rely on Git/manual recovery if they need to revert live changes.
