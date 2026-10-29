# LTFS Tape Manager

Web-based LTFS tape manager for TrueNAS SCALE. The manager runs as a Docker-based TrueNAS Custom App and starts the Aurora LTFS worker on demand to mount and unmount an LTFS tape.

## Requirements

- TrueNAS SCALE with Custom App (Install via YAML) support
- A supported tape drive recognized by TrueNAS
- Four storage locations (datasets or directories), with appropriate permissions
- Access to GitHub Container Registry for the manager and Aurora LTFS images

## Prepare four storage locations

Create or choose **four** storage locations. Their names are examples, not mandatory; you may use any pool name and directory names.

| Purpose | Example TrueNAS path | Usage |
| --- | --- | --- |
| Application data | `/mnt/Pool1/LTFS-Manager` | Persistent configuration and operation state |
| LTFS mountpoint | `/mnt/Pool1/LTFS` | Where the tape filesystem is mounted |
| Write source | `/mnt/Pool1/LTFS-WRITE` | Files available to write to tape |
| Restore destination | `/mnt/Pool1/LTFS-READ` | Where restored files are saved |

**Fresh-install prerequisite:** Create all four host storage locations before installing, using the TrueNAS Datasets interface or directories inside existing datasets. In particular, `/mnt/Pool1/LTFS-Manager` must already exist: the YAML init service creates only its `simple-state-1.5` child. The other three locations (`LTFS`, `LTFS-WRITE`, `LTFS-READ`) must also exist. Substitute your actual pool name for `Pool1`. On TrueNAS datasets with restrictive NFSv4 ACLs, do not use `chmod`/`chown` to work around failures. Ensure the manager can write to the state and restore locations. If you use ordinary directories instead of datasets, you can create them with `sudo mkdir -p /mnt/Pool1/{LTFS-Manager,LTFS,LTFS-WRITE,LTFS-READ}` after verifying `/mnt/Pool1` is your existing pool mountpoint.

The LTFS mountpoint must be suitable for a FUSE mount. Avoid storing ordinary files in the mountpoint. Existing tape data is not erased by installing the application.

## Find your tape-drive device paths

Open **TrueNAS Shell** and run:

```bash
sudo lsscsi -g
```

Find the row whose device type is `tape`, for example:

```text
[0:0:1:0] tape HP Ultrium 6-SCSI 338W /dev/st0 /dev/sg2
```

Here `/dev/sg2` is the **SCSI generic tape device**, not necessarily the same number on your system. Do not use the `/dev/sg*` entry belonging to an enclosure. The non-rewinding tape device is typically `/dev/nst0` for `/dev/st0`; confirm it exists on your TrueNAS system. Device numbering may change if hardware changes.

## Install on TrueNAS SCALE

1. First verify all four host directories exist, including the parent `LTFS-Manager`; the YAML cannot create a missing dataset or parent bind mount when `create_host_path: false` is set.\n2. Open [truenas-custom-app.yaml](truenas-custom-app.yaml) and copy its contents.
3. **Before installing**, edit the YAML to match your four storage paths. Update both the environment values and the corresponding bind-mount `source` paths:
   - `HOST_TAPE_PATH` and the bind mount targeting `/tape`
   - `HOST_WRITE_PATH` and the bind mount targeting `/write-source`
   - `HOST_READ_PATH` and the bind mount targeting `/destination`
   - The bind mount targeting `/state-simple` (your existing `simple-state` directory)
4. Set `SG_DEVICE` and `NST_DEVICE` in the `environment` section to your actual tape devices. **Also update both matching entries under `devices:`**. For example, if your tape uses `/dev/sg4` and `/dev/nst1`:

   ```yaml
   environment:
     SG_DEVICE: /dev/sg4
     NST_DEVICE: /dev/nst1
   devices:
     - /dev/sg4:/dev/sg4:rwm
     - /dev/nst1:/dev/nst1:rwm
   ```

   This snippet shows only the settings to change; keep the rest of the supplied YAML intact.
5. Optionally change the web port in `ports:` (default `8084:8080`; change only the host-side `8084`).
6. In TrueNAS, go to **Apps → Discover Apps → Install via YAML**, use an app name such as `ltfs-tape-manager`, paste your edited YAML, and install.
7. Open `http://YOUR-TRUENAS-IP:8084` (or your chosen host port). Confirm the drive is detected, then test mounting a known LTFS tape **read-only** before performing any write operation.

The stable 1.5.0 manager image is `ghcr.io/geraldjust/ltfs-tape-manager:1.5.0`. It creates and removes the Aurora LTFS worker container on demand; do **not** define a second permanent Aurora worker service in the YAML.

**Safety:** Mounting an existing tape read-only is a useful first test. Formatting a tape destroys its previous LTFS contents; do not select Format unless that is intentional.

## License

Licensed under the **BSD 3-Clause License**. See [LICENSE](LICENSE).
