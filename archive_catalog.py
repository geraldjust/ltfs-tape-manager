"""Archive catalog foundation for LTO Archive Manager 1.6.

Only manages metadata. Never reads or writes tape payloads.
Library removal is a soft removal: on-disk library folders are never deleted.
"""
import re
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")

def utcnow():
    return datetime.now(timezone.utc).isoformat()

class ArchiveCatalog:
    def __init__(self, db_path, archive_root):
        self.db_path = Path(db_path)
        self.root = Path(archive_root)
        if not self.root.is_dir():
            raise RuntimeError("Archive dataset not mounted: " + str(self.root))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS libraries (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL COLLATE NOCASE UNIQUE,
              directory TEXT NOT NULL UNIQUE,
              master_path TEXT,
              active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tapes (
              id TEXT PRIMARY KEY,
              library_id TEXT NOT NULL REFERENCES libraries(id),
              barcode TEXT NOT NULL,
              volume_uuid TEXT,
              registered_at TEXT NOT NULL,
              UNIQUE(library_id,barcode)
            );
            CREATE TABLE IF NOT EXISTS tape_files (
              tape_id TEXT NOT NULL REFERENCES tapes(id),
              relative_path TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,
              mtime_ns INTEGER,
              sha256 TEXT,
              verified INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(tape_id,relative_path)
            );
            """)

    def list_libraries(self, include_removed=False):
        with self.connect() as db:
            sql = "SELECT * FROM libraries" + ("" if include_removed else " WHERE active=1") + " ORDER BY name COLLATE NOCASE"
            return [dict(r) for r in db.execute(sql)]

    def add_library(self, name):
        name = name.strip()
        if not NAME_RE.fullmatch(name) or name in (".", ".."):
            raise ValueError("Invalid library name (1–80 safe characters)")
        target = self.root / name
        if target.is_symlink():
            raise ValueError("Library directory must not be a symlink")
        if target.exists() and not target.is_dir():
            raise ValueError("Library path already exists and is not a directory")
        ident, now = str(uuid.uuid4()), utcnow()
        with self.connect() as db:
            existing = db.execute("SELECT id,active FROM libraries WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            if existing:
                if existing["active"]:
                    raise ValueError("Library already managed")
                db.execute("UPDATE libraries SET active=1,updated_at=? WHERE id=?", (now,existing["id"]))
                ident=existing["id"]
            else:
                db.execute("INSERT INTO libraries(id,name,directory,created_at,updated_at) VALUES(?,?,?,?,?)",
                           (ident,name,name,now,now))
            target.mkdir(exist_ok=True)
        return ident

    def remove_library(self, ident):
        with self.connect() as db:
            result=db.execute("UPDATE libraries SET active=0,updated_at=? WHERE id=? AND active=1",
                              (utcnow(),ident))
            if not result.rowcount:
                raise KeyError("Active library not found")
        # Intentionally never unlink/rmdir or remove tape registrations.
        return True
