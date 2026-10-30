"""Read-only comparison of a library's master source with registered LTFS tape metadata."""
import os
import stat
from pathlib import Path

MAX_FILES=200000
MAX_RESULTS=200000
SKIP_NAMES={".DS_Store","Thumbs.db"}

def scan_master(catalog, library_id, source_root, host_root):
    source_root=Path(source_root).resolve(strict=True)
    with catalog.connect() as db:
        library=db.execute("SELECT * FROM libraries WHERE id=? AND active=1",(library_id,)).fetchone()
        if library is None:
            raise KeyError("Active library not found")
        master=library["master_path"]
        if not master:
            raise ValueError("Select a Master Dataset directory before scanning")
        host_root=host_root.rstrip("/")
        if master!=host_root and not master.startswith(host_root+"/"):
            raise ValueError("Master Dataset is outside the allowed source mount")
        relative=master[len(host_root):].lstrip("/")
        source=(source_root/relative).resolve(strict=True)
        if not source.is_relative_to(source_root) or not source.is_dir():
            raise ValueError("Master Dataset directory is unavailable or outside allowed source")
        inventory={}
        for row in db.execute("""SELECT f.relative_path,f.size_bytes,t.barcode,t.source_prefix
            FROM tape_files f JOIN tapes t ON f.tape_id=t.id
            WHERE t.library_id=? AND t.active=1""",(library_id,)):
            prefix=row["source_prefix"].strip("/")
            full=row["relative_path"]
            if prefix:
                if not full.startswith(prefix+"/"):
                    continue
                full=full[len(prefix)+1:]
            inventory.setdefault(full,[]).append(row)
    counts={"archived":0,"new":0,"modified":0,"metadata_mismatch":0}
    samples=[]
    scanned=0
    for base,dirs,names in os.walk(source,topdown=True,followlinks=False):
        parent=Path(base)
        dirs[:]=[d for d in dirs if not (parent/d).is_symlink()]
        for name in names:
            if name in SKIP_NAMES or name.startswith("._"):
                continue
            p=parent/name
            st=p.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            scanned+=1
            if scanned>MAX_FILES:
                raise ValueError("Scan exceeds 200,000 files; select a smaller Master Dataset")
            rel=p.relative_to(source).as_posix()
            matches=inventory.get(rel,[])
            same_size=[r for r in matches if r["size_bytes"]==st.st_size]
            status="archived" if same_size else ("modified" if matches else "new")
            counts[status]+=1
            if status!="archived" and len(samples)<MAX_RESULTS:
                samples.append({"path":rel,"status":status,"size_bytes":st.st_size,
                    "tapes":[r["barcode"] for r in matches]})
    samples.sort(key=lambda r:r["path"].casefold())
    return {"master_path":master,"counts":counts,"total":scanned,"files":samples,
            "truncated":counts["new"]+counts["modified"]>len(samples),"comparison":"relative_path_and_size",
            "note":"Filename/path and size catalog matching only; no timestamps or content hashes are checked"}
