# LTFS Tape Manager

Web-based LTFS tape manager for TrueNAS SCALE. Install it as a TrueNAS Custom App; the manager starts and stops its Aurora LTFS worker automatically.

## Requirements

- TrueNAS SCALE with **Apps → Discover Apps → Install via YAML**
- A supported tape drive detected by TrueNAS
- A TrueNAS pool with directories/datasets for application data, write sources and restores
- Access to GitHub Container Registry for the manager and Aurora LTFS images

## 1. Prepare the storage locations

**IMPORTANT: `Pool1` is only an example TrueNAS pool name.** Replace **every** occurrence of `/mnt/Pool1/` in the instructions **and YAML** with your own pool's mountpoint, such as `/mnt/MAIN_Pool/`. Do not create a new pool named Pool1 just to match the example.

Use this layout:

```text
/mnt/Pool1/
├── LTFS-Manager/
│   ├── simple-state-1.5/  # Persistent application state
│   └── tape/              # Empty LTFS FUSE mountpoint
├── LTFS-WRITE/            # Files available to write to tape
└── LTFS-READ/             # Destination for restored files
```

**Both `simple-state-1.5` and `tape` MUST be created directly INSIDE `LTFS-Manager`.** They must not be created as siblings of `LTFS-Manager` under the pool root. The `tape` mountpoint should be an **ordinary empty directory**, not a separate ZFS dataset. Keep unrelated files out of it, and avoid backup/snapshot or indexing jobs traversing the mounted tape.

You can create the top-level storage locations in **TrueNAS → Storage → Datasets**, or use ordinary directories inside your existing pool/datasets. Then create the two subfolders **inside `LTFS-Manager`**, either manually in the TrueNAS interface or using the shell.

For example, **only after substituting your actual pool name**, create all required directories with:

```bash
sudo mkdir -p \
  /mnt/Pool1/LTFS-Manager/simple-state-1.5 \
  /mnt/Pool1/LTFS-Manager/tape \
  /mnt/Pool1/LTFS-WRITE \
  /mnt/Pool1/LTFS-READ
```

Verify the directories before installing:

```bash
for p in \
  /mnt/Pool1/LTFS-Manager/simple-state-1.5 \
  /mnt/Pool1/LTFS-Manager/tape \
  /mnt/Pool1/LTFS-WRITE \
  /mnt/Pool1/LTFS-READ
do
  if [ -d "$p" ]; then echo "OK: $p"; else echo "MISSING: $p"; fi
done
```

All four checks must report `OK`. Docker validates bind-mount source paths **before** starting the app, so an init service cannot reliably create a missing directory during the same deployment. The supplied YAML uses `create_host_path: false` to fail clearly rather than silently create a wrong path. On TrueNAS paths protected by NFSv4 ACLs, **do not use `chmod` or `chown`** as a workaround. Ensure the app has suitable access to its state and restore paths.

## 2. Find the tape-drive device paths

In TrueNAS Shell, run:

```bash
sudo lsscsi -g
ls -l /dev/nst0
```

Example:

```text
[9:0:0:0] tape HP Ultrium 6-SCSI J5EZ /dev/st0 /dev/sg1
```

The tape device in this example is `/dev/sg1`; **your number may be different**. Do not select the `/dev/sg*` device for an enclosure. Confirm the non-rewinding tape device, commonly `/dev/nst0`. Tape device numbers can change after hardware changes or a reinstall.

## 3. Install on TrueNAS SCALE

1. Confirm the required directories exist, including **both** `LTFS-Manager/simple-state-1.5` and `LTFS-Manager/tape`. The `tape` directory must be empty before mounting.
2. Open [the 1.5.0 Custom App YAML](truenas-custom-app.yaml) and copy its complete contents.
3. **Replace `Pool1` everywhere in the YAML with your actual pool name.** If you chose different folder names, update all matching environment variables and bind-mount sources:
   - `HOST_TAPE_PATH` and the bind mount with `target: /tape` must both point to `/mnt/YOUR-POOL/LTFS-Manager/tape`.
   - `HOST_WRITE_PATH` and `target: /write-source` must point to the same host write-source folder.
   - `HOST_READ_PATH` and `target: /destination` must point to the same host restore folder.
   - The bind mount with `target: /state-simple` must point to `/mnt/YOUR-POOL/LTFS-Manager/simple-state-1.5`.
4. Update `SG_DEVICE` and `NST_DEVICE` **and both corresponding `devices:` mappings** to match your drive. For example, if your drive is `/dev/sg4` and `/dev/nst1`:

   ```yaml
   environment:
     SG_DEVICE: /dev/sg4
     NST_DEVICE: /dev/nst1
   devices:
     - /dev/sg4:/dev/sg4:rwm
     - /dev/nst1:/dev/nst1:rwm
   ```

   This is a partial example, not a replacement for the full YAML.
5. The default GUI port is `8084` (`8084:8080`). If necessary, change only the host-side port.
6. In **Apps → Discover Apps → Install via YAML**, enter a name such as `ltfs-tape-manager-1-5`, paste the edited YAML and install.
7. Open `http://YOUR-TRUENAS-IP:8084`. Confirm **GUI version 1.5.0**, drive detection, and tape presence. Mount an existing LTFS cartridge **read-only** first; then unmount it cleanly.

The manager image is `ghcr.io/geraldjust/ltfs-tape-manager:1.5.0`; the Aurora image is `ghcr.io/auroratape/aurora-ltfs:1.5.0`. The manager creates and removes the Aurora worker **on demand**. Do **not** add a permanent Aurora worker service or run multiple managers against the same drive.

## 4. Optional installation checks

After installation, check the manager and its backend version:

```bash
sudo docker ps --filter name=ltfs-manager-1-5
sudo docker exec ltfs-manager-1-5 python -c "import app; print(app.VERSION)"
```

While a tape is mounted read-only, check the host mountpoint and on-demand worker (substitute your pool name):

```bash
findmnt /mnt/Pool1/LTFS-Manager/tape
sudo docker ps --filter name=aurora-ltfs-1-5
```

A successful read-only mount shows `ltfs:/dev/sgX`, filesystem `fuse`, and the `ro` option. After unmounting, `findmnt` should return no mount for that path and the worker should stop. An empty drive should show **Drive ready — No tape**; a missing Aurora worker while unmounted is normal.

**Safety:** Installing the manager does not format a cartridge. Formatting a tape destroys its previous LTFS contents; only use Format when intentionally erasing test media. Complete a read-only mount/unmount test before attempting a write.

## License

Licensed under the **BSD 3-Clause License**. See [LICENSE](LICENSE).
