"""Streaming, uncompressed TAR archives for LTFS v1.7.1 development.

Archives and JSON manifests are stored side by side on LTFS. No HDD staging.
Archive members use basename-only names; parent folder is represented by the
archive's LTFS directory. All extraction paths are independently validated.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

class HashingWriter:
    def __init__(self, raw, callback=None):
        self.raw, self.digest, self.count, self.callback = raw, hashlib.sha256(), 0, callback
    def write(self, data):
        n = self.raw.write(data)
        if n != len(data):
            raise IOError("Short LTFS archive write")
        self.digest.update(data)
        self.count += n
        if self.callback:
            self.callback(self.count)
        return n
    def flush(self):
        self.raw.flush()

class HashingReader:
    def __init__(self, raw, callback=None):
        self.raw, self.digest, self.count, self.callback = raw, hashlib.sha256(), 0, callback
    def read(self, size=-1):
        data = self.raw.read(size)
        self.digest.update(data)
        self.count += len(data)
        if self.callback:
            self.callback(self.count)
        return data

class MemberReader:
    def __init__(self, path, expected_size, expected_mtime):
        self.path, self.remaining = path, expected_size
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Unsafe TAR source: " + str(path))
        self.raw = open(path, "rb")
        self.hash = hashlib.sha256()
        st = path.stat()
        if st.st_size != expected_size or st.st_mtime_ns != expected_mtime:
            self.raw.close()
            raise RuntimeError("Source changed before TAR write: " + str(path))
    def read(self, n=-1):
        if n < 0 or n > self.remaining:
            n = self.remaining
        data = self.raw.read(n)
        if len(data) != n:
            raise IOError("Source truncated during TAR write: " + str(self.path))
        self.hash.update(data)
        self.remaining -= len(data)
        return data
    def close(self):
        self.raw.close()

def safe_member_name(name):
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("Invalid TAR member")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Unsafe TAR member: " + name)
    return name

def create_archive(tape_path, manifest_path, entries, callback=None):
    """entries are (relative path, source Path, size, mtime_ns).
    Write the manifest only after a complete archive write. Caller commits LTFS.
    """
    if tape_path.exists() or manifest_path.exists():
        raise FileExistsError("Archive or manifest already exists")
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"schema": 1, "archive": tape_path.name, "members": []}
    try:
        with open(tape_path, "xb") as output:
            sink = HashingWriter(output, callback)
            with tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for rel, src, size, mtime in entries:
                    name = safe_member_name(Path(rel).name)
                    reader = MemberReader(src, size, mtime)
                    try:
                        info = tarfile.TarInfo(name)
                        info.size = size
                        info.mtime = int(mtime / 1_000_000_000)
                        info.mode = 0o644
                        tar.addfile(info, reader)
                        if reader.remaining:
                            raise IOError("Incomplete TAR member: " + rel)
                        if src.stat().st_size != size or src.stat().st_mtime_ns != mtime:
                            raise RuntimeError("Source changed during TAR write: " + rel)
                        manifest["members"].append({"name": name, "size": size,
                                                    "sha256": reader.hash.hexdigest()})
                    finally:
                        reader.close()
            output.flush()
            os.fsync(output.fileno())
        manifest["archive_size"] = sink.count
        manifest["archive_sha256"] = sink.digest.hexdigest()
        with open(manifest_path, "x", encoding="utf-8") as f:
            json.dump(manifest, f, separators=(",", ":"), ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        return manifest
    except Exception:
        # Leave partial tape files for inspection; never overwrite them on retry.
        raise

def load_manifest(tape_path):
    path = tape_path.with_name(tape_path.name + ".manifest.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or data.get("archive") != tape_path.name:
        raise ValueError("Invalid TAR manifest")
    if not isinstance(data.get("members"), list) or not isinstance(data.get("archive_size"), int):
        raise ValueError("Incomplete TAR manifest")
    if not isinstance(data.get("archive_sha256"), str) or len(data["archive_sha256"]) != 64:
        raise ValueError("Missing TAR archive hash")
    seen = set()
    for member in data["members"]:
        name = safe_member_name(member["name"])
        if name in seen or type(member["size"]) is not int or member["size"] < 0:
            raise ValueError("Invalid or duplicate TAR member")
        seen.add(name)
        if not isinstance(member.get("sha256"), str) or len(member["sha256"]) != 64:
            raise ValueError("Missing member hash")
    if tape_path.stat().st_size != data["archive_size"]:
        raise ValueError("TAR size does not match manifest")
    return data

def extract_members(tape_path, out_root, selected_names=None, callback=None):
    """Read full TAR sequentially and verify archive plus extracted member hashes.

    selected_names=None means all. No links, special entries, or unsafe paths.
    Caller must create a new, empty destination root.
    """
    manifest = load_manifest(tape_path)
    known = {x["name"]: x for x in manifest["members"]}
    selected = set(known) if selected_names is None else set(selected_names)
    if not selected or not selected.issubset(known):
        raise ValueError("Unknown TAR selection")
    observed = set()
    with open(tape_path, "rb") as raw:
        source = HashingReader(raw, callback)
        with tarfile.open(fileobj=source, mode="r|*") as archive:
            for member in archive:
                name = safe_member_name(member.name)
                if not member.isfile() or name not in known or name in observed:
                    raise ValueError("Unexpected TAR member type or name")
                observed.add(name)
                if member.size != known[name]["size"]:
                    raise ValueError("TAR member size mismatch")
                if name not in selected:
                    continue
                destination = out_root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if any(p.is_symlink() for p in [destination.parent, *destination.parent.parents] if p != Path('/')):
                    raise ValueError("Unsafe TAR destination directory")
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError("Restore path collision: " + str(destination))
                digest = hashlib.sha256()
                source_member = archive.extractfile(member)
                if source_member is None:
                    raise IOError("Cannot read TAR member")
                with open(destination, "xb") as output:
                    remaining = member.size
                    while remaining:
                        block = source_member.read(min(4 * 1024 * 1024, remaining))
                        if not block:
                            raise IOError("Truncated TAR member")
                        output.write(block)
                        digest.update(block)
                        remaining -= len(block)
                if digest.hexdigest() != known[name]["sha256"]:
                    raise ValueError("Restored TAR member SHA-256 mismatch: " + name)
                # Independently re-read destination before marking verified.
                check = hashlib.sha256()
                with open(destination, "rb") as f:
                    for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
                        check.update(block)
                if check.hexdigest() != digest.hexdigest():
                    raise ValueError("Destination SHA-256 mismatch: " + name)
        # tarfile's streaming iterator may not consume end padding.
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            pass
    if observed != set(known):
        raise ValueError("TAR contents differ from manifest")
    if source.count != manifest["archive_size"] or source.digest.hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Committed TAR archive SHA-256 mismatch")
    return len(selected)
