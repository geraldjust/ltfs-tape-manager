"""Idempotent catalog update for committed, SHA-256-verified direct-file tape writes."""
import os
from pathlib import Path
from archive_catalog import utcnow

def sync_verified(catalog, library_id, barcode, entries):
    """entries: [{path,size_bytes,mtime_ns,sha256}]. Never replace existing placeholders."""
    if not entries:
        raise ValueError("No verified files to register")
    with catalog.connect() as db:
        lib=db.execute("SELECT directory FROM libraries WHERE id=? AND active=1",(library_id,)).fetchone()
        if not lib:raise ValueError("Archive library is missing or inactive")
        tape=db.execute("SELECT id FROM tapes WHERE library_id=? AND barcode=?",(library_id,barcode)).fetchone()
        if not tape:raise ValueError("Tape barcode is not registered in the selected library")
        target=catalog.root/lib["directory"]/barcode
        if target.is_symlink() or not target.is_dir():raise ValueError("Archive barcode directory missing or unsafe")
        tape_id=tape["id"]
        prepared=[]
        for item in entries:
            rel=item["path"]
            if not isinstance(rel,str) or not rel or rel.startswith("/") or "\\" in rel or any(p in ("",".","..") for p in rel.split("/")):
                raise ValueError("Unsafe archive path")
            size=int(item["size_bytes"])
            if size<0:raise ValueError("Invalid archive file size")
            digest=item["sha256"]
            if not isinstance(digest,str) or len(digest)!=64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("Invalid verified SHA-256")
            path=target/rel
            parent=target
            for part in Path(rel).parts[:-1]:
                parent=parent/part
                if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                    raise ValueError("Unsafe archive parent: "+rel)
            if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size!=0)):
                raise ValueError("Archive placeholder conflict: "+rel)
            existing=db.execute("SELECT size_bytes,sha256 FROM tape_files WHERE tape_id=? AND relative_path=?",(tape_id,rel)).fetchone()
            if existing and (existing["size_bytes"]!=size or (existing["sha256"] and existing["sha256"]!=digest)):
                raise ValueError("Catalog conflict: "+rel)
            prepared.append((rel,size,item.get("mtime_ns"),digest,path))
        # Idempotent retry: create missing placeholders, then update metadata in a DB transaction.
        for rel,size,mtime,digest,path in prepared:
            path.parent.mkdir(parents=True,exist_ok=True)
            if not path.exists():
                try:
                    with path.open("xb"):pass
                except FileExistsError:
                    if path.is_symlink() or not path.is_file() or path.stat().st_size!=0:raise
            if mtime is not None:
                try:os.utime(path,ns=(int(mtime),int(mtime)),follow_symlinks=False)
                except OSError:pass
        for rel,size,mtime,digest,path in prepared:
            db.execute("""INSERT INTO tape_files(tape_id,relative_path,size_bytes,mtime_ns,sha256,verified,verified_at)
                       VALUES(?,?,?,?,?,1,?)
                       ON CONFLICT(tape_id,relative_path) DO UPDATE SET
                       verified=1,sha256=excluded.sha256,verified_at=excluded.verified_at
                       WHERE tape_files.size_bytes=excluded.size_bytes""",
                       (tape_id,rel,size,mtime,digest,utcnow()))
        return {"files":len(prepared),"barcode":barcode,"library_id":library_id}
