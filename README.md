# LTO Archive Manager — TrueNAS SCALE (1.6 development branch)

A web-based manager for LTFS tape drives on TrueNAS SCALE. Version **1.6.0.8-dev** adds archive libraries and a multi-tape file catalog to the familiar tape Status, Read, Write and Format workflows. The manager starts an Aurora LTFS worker only when needed.

> **Development release:** The `1.6` branch uses `ghcr.io/geraldjust/ltfs-tape-manager:1.6.0.8-dev`. Review the configuration and back up the persistent state before upgrading. Aurora LTFS currently uses `ghcr.io/auroratape/aurora-ltfs:1.5.0`.

## Requirements

- TrueNAS SCALE with **Apps → Discover Apps → Install via YAML**
- A compatible tape drive visible to Linux (check with `sudo lsscsi -g`)
- Storage for persistent manager state, tape mountpoint, write sources, restores, and an archive library tree
- Access to GitHub Container Registry to pull the manager and Aurora images

## 1. Prepare storage

`Pool1` is a **placeholder**. Replace **every** `/mnt/Pool1/` in commands and the [Custom App YAML](truenas-custom-app.yaml) with your real pool mountpoint.

```text
/mnt/Pool1/
├── LTFS-Manager/
│   ├── simple-state-1.6/   # Persistent libraries, catalog, tape records, settings
│   └── tape/               # Empty ordinary directory for LTFS FUSE mount
├── LTFS-WRITE/             # Source files for direct tape writes
├── LTFS-READ/              # Restore destination
└── LTO-Archive/            # Archive library tree (separate dataset recommended)
```

Create the directories **before** installing. For example, after changing the pool name:

```bash
sudo mkdir -p \
  /mnt/Pool1/LTFS-Manager/simple-state-1.6 \
  /mnt/Pool1/LTFS-Manager/tape \
  /mnt/Pool1/LTFS-WRITE \
  /mnt/Pool1/LTFS-READ \
  /mnt/Pool1/LTO-Archive
```

**Important:** `simple-state-1.6` and `tape` must be *inside* `LTFS-Manager`. The `tape` path must be an **ordinary empty directory**, **not** a separate ZFS dataset. Do **not** share `LTFS-Manager` or its `tape` mountpoint over SMB. The nested FUSE filesystem can conflict with the parent dataset's NFSv4 ACL and extended-attribute behavior. If SMB access is needed, share a separate suitable dataset such as `LTO-Archive` instead.

Do not use `chmod` or `chown` as a workaround on TrueNAS paths protected by NFSv4 ACLs. Configure access through TrueNAS as appropriate. Avoid snapshot, backup, and indexing jobs traversing the live `tape` mountpoint. The YAML uses `create_host_path: false`, so missing host paths must be created ahead of time.

## 2. Identify the tape devices

In the TrueNAS shell:

```bash
sudo lsscsi -g
ls -l /dev/nst* /dev/sg* 2>/dev/null
```

Example:

```text
[2:0:0:0] tape HP Ultrium 6-SCSI 338W /dev/st0 /dev/sg1
```

Here the tape's generic SCSI device is `/dev/sg1` and its non-rewinding tape device is `/dev/nst0`. **Do not assume these names remain fixed after a reboot.** Confirm that the selected `/dev/sgX` is the **tape** device, not an enclosure or disk. If the numbering changes, update both the environment variables and the Docker device mappings, then redeploy.

## 3. Install as a TrueNAS Custom App

1. Open [`truenas-custom-app.yaml`](truenas-custom-app.yaml) from this **1.6 branch**.
2. Replace every `/mnt/Pool1/` with your real pool path. Ensure `HOST_ARCHIVE_PATH`, `HOST_TAPE_PATH`, `HOST_WRITE_PATH`, and `HOST_READ_PATH` match the corresponding bind mounts.
3. Set `SG_DEVICE` and `NST_DEVICE` to the paths confirmed above, and change both entries under `devices:` to match. The included `/dev/sg1` and `/dev/nst0` are **examples**, not universal defaults.
4. Keep the persistent bind mount `/mnt/YOUR-POOL/LTFS-Manager/simple-state-1.6:/state-simple`. Keep `/mnt/YOUR-POOL/LTFS-Manager/tape` mapped to `/tape` with `rshared` propagation.
5. Install using **Apps → Discover Apps → Install via YAML**, with a name such as `lto-archive-manager-1-6`.
6. Open `http://YOUR-TRUENAS-IP:8084` (default mapping `8084:8080`). Confirm the displayed GUI version is `1.6.0.8-dev`, check drive detection, and test mounting an existing LTFS tape **read-only**.

The manager container is `lto-archive-manager-1-6`; the on-demand worker is `aurora-ltfs-1-6`. Do not run multiple managers or a separate permanent Aurora worker against the same drive.

## 4. Using archive libraries

The **Archive** workflow lets you organize a master source folder as a library and track files across multiple registered LTFS tapes.

1. Configure or select the library and its master source folder.
2. Register the tape you intend to use, using its identity/barcode, and associate it with the correct library.
3. Scan the source to compare files with the library catalog. Review the reported new, previously archived, and modified items.
4. Review the proposed selection for the inserted tape. Capacity planning is designed to stop at a conservative **90%** of tape capacity; check the estimate and free space before confirming.
5. Start the write only after reviewing the selection and confirmations. Allow the write, index commit, and verification to finish before ejecting.
6. When a tape is full, insert and register the next tape, then repeat the scan/selection workflow for remaining files.

The catalog records which tape contains each archived file and maintains verification information. **Do not delete or recreate the state folder between tapes.** The catalog is an index, not a replacement for the data physically stored on cartridges. Preserve tape labels/barcodes and keep an independent backup of the state directory.

## 5. Read, write, verify, and eject

- **Status:** Check drive, inserted tape, mount state, capacity, and operation status.
- **Read:** Mount LTFS read-only, browse tape contents, choose a destination under the configured restore path, and restore the selected files.
- **Write:** Select source files, review the operation, and write to an LTFS tape. Follow progress and completion/verification results.
- **Format:** Explicitly initialize a cartridge only when intended. **Formatting destroys the tape's previous LTFS contents.**
- **Eject:** Use the application's clean unmount/eject workflow rather than pulling a mounted tape.

SHA-256 verification and archive catalog status help distinguish recorded data from verified data. Do not assume a file is safely archived solely because a write was started; check the final operation result and catalog status.

### Write-protected cartridges (1.6.0.8-dev)

When Aurora explicitly reports that a cartridge is write-protected, the Write tab displays a warning and disables write actions. A read-only mount **by itself does not prove** that the physical write-protect switch is set, so the warning may not appear until Aurora detects protection. If a write attempt encounters confirmed protection, the manager is designed to fail promptly instead of waiting the full mount timeout. Eject, unlock the cartridge's physical write-protect switch, reinsert, and remount before trying again.

## 6. Upgrading safely within 1.6

**An image update is not a fresh installation.** To update an existing `1.6.0.7-dev` deployment to `1.6.0.8-dev`:

1. Finish current tape operations and unmount cleanly.
2. Back up the complete `simple-state-1.6` directory while the application is stopped or otherwise quiescent.
3. Preserve **all existing volume mappings**, especially the state path mapped to `/state-simple` and the archive source path mapped to `/archive`.
4. Keep the verified SCSI device paths; recheck `sudo lsscsi -g` after any TrueNAS reboot.
5. Change only the manager image tag to `1.6.0.8-dev` and redeploy.
6. Verify the displayed GUI/backend version, existing library list, tape registrations, catalog counts, and a read-only mount before starting a new write.

**Never delete `simple-state-1.6` to upgrade.** It contains library definitions, catalog entries, tape associations, and verification records. A different or empty state path can make existing libraries appear to disappear even though the tapes still hold their data.

## 7. Troubleshooting

**Aurora fails to open `/dev/sgX` after a reboot:** Run `sudo lsscsi -g`. Linux may have reassigned SCSI generic device numbers; an old path may now identify an enclosure rather than the tape drive. Update `SG_DEVICE` and the matching `devices:` mapping, and check `NST_DEVICE` as well.

**Mountpoint missing or stale:** Check that `LTFS-Manager/tape` exists as an ordinary directory and that the bind mount is configured correctly. Do not delete a live mounted directory or run a second LTFS instance against the drive.

**Archive library appears empty after an update:** Confirm the *same* host `simple-state-1.6` path is still mounted at `/state-simple`. Do not initialize a replacement library or format tapes as a troubleshooting step.

**Tape mounts read-only when writing is requested:** Check the physical cartridge write-protect switch and Aurora logs. A read-only mount is normal for browsing; protection must be confirmed separately.

**Check containers:**

```bash
sudo docker ps -a --filter name=lto-archive-manager-1-6
sudo docker ps -a --filter name=aurora-ltfs-1-6
sudo docker logs --tail 100 lto-archive-manager-1-6
```

The Aurora worker being absent or stopped while no tape is mounted is normal.

## License

BSD 3-Clause License — see [LICENSE](LICENSE).
