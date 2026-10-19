"""v1.7.1 TAR planning primitives. Not connected to tape operations yet.

No archive is written by this module. All paths are relative to source root.
"""
from collections import defaultdict
from pathlib import Path
import re

MIB = 1024 ** 2
GIB = 1024 ** 3
THRESHOLDS_MIB = (1, 4, 16, 64, 256)
ARCHIVE_LIMITS_GIB = (1, 2, 4, 8, 16, 32)

def tar_entry_bytes(size, name):
    """Conservative estimate for standard TAR entry plus PAX overhead."""
    # PAX extended header accommodates long UTF-8 names and metadata.
    return 4096 + ((size + 511) // 512) * 512

def plan_archives(files, threshold_mib=16, max_archive_gib=32):
    """Group immediate sibling files into bounded, uncompressed TAR archives.

    files: iterable of (relative_path, byte_size); returns
    (standalone list, archive list). Never mix folders.
    """
    if type(threshold_mib) is not int or threshold_mib not in THRESHOLDS_MIB:
        raise ValueError("Unsupported small-file threshold")
    if type(max_archive_gib) is not int or max_archive_gib not in ARCHIVE_LIMITS_GIB:
        raise ValueError("Unsupported maximum archive size")
    threshold = threshold_mib * MIB
    limit = max_archive_gib * GIB
    standalone = []
    groups = defaultdict(list)
    seen = set()
    for rel, size in files:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
            raise ValueError("Unsafe relative path")
        p = Path(rel)
        if any(x in (".", "..") for x in rel.split("/")) or p.name in ("", ".", ".."):
            raise ValueError("Unsafe relative path")
        if rel in seen:
            raise ValueError("Duplicate path")
        seen.add(rel)
        if type(size) is not int or size < 0:
            raise ValueError("Invalid file size")
        if size >= threshold:
            standalone.append((rel, size))
        else:
            groups[str(p.parent)].append((rel, size))
    archives = []
    reserved = set(seen)
    for parent, members in sorted(groups.items()):
        batch = []
        estimate = 10240
        index = 0
        def add_batch():
            nonlocal batch, estimate, index
            if not batch:
                return
            while True:
                index += 1
                candidate = str(Path(parent) / ("small-files-%03d.tar" % index))
                if candidate not in reserved and candidate + ".manifest.json" not in reserved:
                    break
            reserved.add(candidate)
            reserved.add(candidate + ".manifest.json")
            archives.append({"path": candidate, "manifest": candidate + ".manifest.json",
                             "members": batch, "estimated_bytes": estimate})
            batch = []
            estimate = 1024
        for rel, size in sorted(members):
            needed = tar_entry_bytes(size, Path(rel).name)
            if needed + 10240 > limit:
                standalone.append((rel, size))
                continue
            if batch and estimate + needed > limit:
                add_batch()
            batch.append((rel, size))
            estimate += needed
        add_batch()
    return sorted(standalone), archives
