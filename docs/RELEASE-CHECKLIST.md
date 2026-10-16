# Public release checklist

Do not merge or publish an image until the actual working application source has been reviewed.

- [ ] Import the verified working application source without private runtime data.
- [ ] Remove tape serial numbers, cartridge barcodes, volume labels, media filenames, and host identifiers.
- [ ] Replace host-specific pool names, mountpoints, and device paths with configurable settings.
- [ ] Exclude local logs, state, credentials, and operation history from the Docker build context.
- [ ] Check that the image contains no personal information or secrets.
- [ ] Verify that GUI and API version strings match the release.
- [ ] Verify the TrueNAS YAML and host mount propagation on a disposable test system.
- [ ] Validate Docker socket permissions and prevent concurrent tape managers.
- [ ] Verify read-only mount, write/index commit, readback verification, and safe shutdown.
- [ ] Only then enable release-triggered GHCR publishing.
- [ ] Mark GHCR package public before advertising anonymous YAML installation.

The existing installation must remain unchanged during release preparation.
