# LTFS Tape Manager — TrueNAS SCALE

**Release preparation:** The image and YAML are not yet validated or ready for installation.

## Required datasets

Create these four datasets in your own pool (replace `example` with the pool name):

- `/mnt/example/LTFS-Manager` — application state
- `/mnt/example/LTFS` — tape mountpoint
- `/mnt/example/LTFS-READ` — restore destination
- `/mnt/example/LTFS-WRITE` — files staged for tape

The `/dev/sgX` and `/dev/nstX` device names in the template are placeholders, not actual device paths. Discover the correct device names for each installation.

## Safety

The manager uses the Docker socket to start a privileged LTFS worker. This grants host-equivalent privileges and must be limited to trusted administrators. Do not run two managers against the same tape drive. A clean LTFS unmount is required after writes to commit the index.

Never format a tape during installation. Do not commit runtime logs, state, tape serials, barcodes, private filenames, or host-specific configuration.

## Publication checklist

The preparation branch contains a manual-only GitHub Actions publishing workflow and an unvalidated TrueNAS YAML template. Before publishing an image, import the sanitized working source, review it, verify that GUI and API versions match, test image building, and validate mount propagation and tape operations.
