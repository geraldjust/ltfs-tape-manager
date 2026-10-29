# LTO Archive Manager — 1.6.0.0 Development Specification

Based on the verified LTFS Tape Manager 1.5.0 release. **Do not change or replace the stable 1.5 branches or images.**

## Non-negotiable compatibility
- All cartridges stay ordinary LTFS volumes. No proprietary on-tape format, no catalog dependency to restore, no required sidecar file on tape.
- Preserve mount, read, format, write, sequential writing, TAR packaging, clean index commit, SHA-256 verification, tape identity and worker lifecycle.
- General-purpose Read/Write remain unchanged. Archive adds an orchestrated workflow using the same verified write engine.
- GUI and backend must both identify the development build as `1.6.0.0-dev`.

## Storage
- Dedicated TrueNAS ZFS dataset: `/mnt/<POOL>/LTO-Archive` (the pool is user-configurable).
- Bind the dataset to the manager at `/archive`; configure `HOST_ARCHIVE_PATH` for any worker-visible host paths.
- Persistent application state and SQLite catalog remain under `/state-simple`; archive dataset contains user-searchable library/barcode directories and 0-byte file placeholders.
- Catalog and placeholder tree should be backed up together, with an export/rebuild process.
- Existing `/tape` FUSE mount remains a normal empty directory inside `LTFS-Manager`, not a separate ZFS dataset.

## Library UX
- Archive tab with library sidebar, add (+), remove (-), selected library details.
- Adding a library prompts for a name, creates its directory, and registers a persistent ID.
- Removing a library always asks for confirmation and **only removes it from management**. The archive dataset directory, barcode folders and placeholder files are retained untouched. Re-adding/relinking should be supported.
- Per-library Master Dataset path chosen via server-side folder browser limited to configured allowed roots; do not rely on browser local file picker.
- Master paths are read-only, validated, and never used as destinations for placeholders.

## Tape registration
- Read-only LTFS mount, obtain barcode or prompt for explicit ID if unavailable, inspect volume identity and prevent conflicting/duplicate registrations.
- Scan tape directory metadata and preserve relative paths; create `/archive/<library>/<barcode>/...` folders and 0-byte placeholder files with original names/extensions.
- Store original file size, modification time (and creation time if available), SHA-256 if known, tape identity, LTFS path, archive state, and scan time in SQLite. Where practical apply original mtime to placeholders, but **never treat placeholder filesystem metadata as authoritative**.
- Do not hash full tape content during registration; tape content should not be copied. Avoid symlink traversal, unsafe paths, collisions, and partial registrations.
- Support rescanning and multiple tapes per library. Register only complete scans; use staging plus atomic promotion for placeholder trees.

## Source comparison
- Scan live Master Dataset recursively and compare against all successfully registered/verified archived versions for the selected library.
- States: New, Modified, Archived, Missing from Master. Compare path/size/mtime first; optional SHA-256 when needed.
- Record historical versions: new content at the same path does not invalidate old tape records.
- Efficient incremental scans, progress and cancellation; no destructive writes to master.

## Archive-aware write
- Select New/Modified files in Archive; `Write Selected to Tape & Register` reuses the established sequential LTFS engine.
- Check capacity, retain source paths, commit index, remount read-only, SHA-256 verify.
- **Only after successful verification** register tape/file versions and generate placeholders. Failures must never appear archived.
- No concurrent operations on same drive; general-purpose Write and Read stay available.

## Delivery sequence
1. Brand and configure 1.6 development without breaking 1.5.
2. Library CRUD, persistent catalog, dataset binding and GUI.
3. Tape registration, placeholder metadata and safe rescan.
4. Master source scan/diff.
5. Archive write orchestration and failure recovery.
6. Multi-tape tests, documentation and stable release.

## Security and recovery
- Do not delete archive dataset content when removing a library from management.
- Explicitly label placeholders as metadata-only, not restored files.
- SQLite transactions, foreign keys, migrations, backup/export and interrupted-operation recovery.
- Restrict folder selection to configured allowed host paths, prevent path traversal and symlink escapes.
- Keep archive data independent from tape recoverability.
