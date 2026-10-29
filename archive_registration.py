"""Read-only LTFS inventory and non-destructive placeholder registration."""
import os
import re
import stat
import uuid
from pathlib import Path

BARCODE_RE=re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_FILES=200000

def scan_tape(root):
    root=Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("LTFS mount is unavailable")
    files=[]
    directories=[]
    for base, dirs, names in os.walk(root, topdown=True, followlinks=False):
        parent=Path(base)
        clean=[]
        for name in dirs:
            p=parent/name
            if p.is_symlink():
                raise ValueError("Tape contains a symlink; registration aborted")
            clean.append(name)
            directories.append(p.relative_to(root))
        dirs[:]=clean
        for name in names:
            p=parent/name
            st=p.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("Tape contains an unsupported file type: "+str(p.relative_to(root)))
            files.append((str(p.relative_to(root)),int(st.st_size),int(st.st_mtime_ns)))
            if len(files)>MAX_FILES:
                raise ValueError("Tape has too many files for this release")
    return directories,files

def register_tape(catalog,library_id,barcode,root):
    if not isinstance(barcode,str) or not BARCODE_RE.fullmatch(barcode):
        raise ValueError("Enter a tape barcode or manual identifier (1-32 letters, digits, - or _)")
    with catalog.connect() as db:
        library=db.execute("SELECT * FROM libraries WHERE id=? AND active=1",(library_id,)).fetchone()
        if not library:
            raise KeyError("Active library not found")
        if db.execute("SELECT 1 FROM tapes WHERE library_id=? AND barcode=?",(library_id,barcode)).fetchone():
            raise ValueError("This tape is already registered in this library")
    directories,files=scan_tape(root)
    library_root=catalog.root/library["directory"]
    if library_root.is_symlink() or not library_root.is_dir():
        raise ValueError("Library folder is missing or unsafe")
    target=library_root/barcode
    if target.exists() or target.is_symlink():
        raise ValueError("Barcode folder already exists; refusing to overwrite existing archive data")
    target.mkdir()
    try:
        for rel in directories:
            p=target/rel
            p.mkdir(exist_ok=True)
        for rel,size,mtime_ns in files:
            p=target/rel
            with p.open('xb'):
                pass
            try:
                os.utime(p,ns=(mtime_ns,mtime_ns),follow_symlinks=False)
            except OSError:
                pass
        tape_id=str(uuid.uuid4())
        from archive_catalog import utcnow
        with catalog.connect() as db:
            db.execute("INSERT INTO tapes(id,library_id,barcode,registered_at) VALUES(?,?,?,?)",
                       (tape_id,library_id,barcode,utcnow()))
            db.executemany("INSERT INTO tape_files(tape_id,relative_path,size_bytes,mtime_ns,verified) VALUES(?,?,?,?,0)",
                           ((tape_id,*f) for f in files))
        return {"tape_id":tape_id,"barcode":barcode,"files":len(files),"directories":len(directories),
                "original_bytes":sum(f[1] for f in files),"verification":"metadata_only"}
    except Exception:
        # Do not delete a partial tree: preserve evidence and prevent silent replacement.
        raise
