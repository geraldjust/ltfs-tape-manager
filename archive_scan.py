"""Read-only comparison of a library's master source with registered LTFS tape metadata."""
import os
import stat
from pathlib import Path

MAX_FILES=200000
MAX_RESULTS=3000
SKIP_NAMES={".DS_Store","Thumbs.db"}
MTIME_TOLERANCE_NS=2_000_000_000

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
        for row in db.execute("""SELECT f.relative_path,f.size_bytes,f.mtime_ns,f.sha256,f.verified,f.verified_source_size,f.verified_source_mtime_ns,t.barcode,t.source_prefix
            FROM tape_files f JOIN tapes t ON f.tape_id=t.id
            WHERE t.library_id=?""",(library_id,)):
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
            archived=[r for r in matches if r["size_bytes"]==st.st_size and
                r["mtime_ns"] is not None and abs(r["mtime_ns"]-st.st_mtime_ns)<=MTIME_TOLERANCE_NS]
            # Verified hashes from a prior run alone are insufficient: source
            # may have changed since then. Compare current source digest when
            # verification is requested, not during a metadata-only scan.
            same_size=[r for r in matches if r["size_bytes"]==st.st_size]
            verified_matches=[r for r in same_size if r["verified"] and r["sha256"] and r["verified_source_size"]==st.st_size and r["verified_source_mtime_ns"]==st.st_mtime_ns]
            status="archived" if (archived or verified_matches) else ("metadata_mismatch" if same_size else ("modified" if matches else "new"))
            details=matches[0] if matches else None
            if same_size and not archived:
                details=(verified_matches or same_size)[0]
            counts[status]+=1
            if len(samples)<MAX_RESULTS:
                samples.append({"path":rel,"status":status,"size_bytes":st.st_size,
                    "mtime_ns":st.st_mtime_ns,
                    "tape_size_bytes":details["size_bytes"] if details else None,
                    "catalog_sha256_verified":bool(verified_matches),
                    "tape_mtime_ns":details["mtime_ns"] if details else None,
                    "mtime_delta_seconds":round(abs(st.st_mtime_ns-details["mtime_ns"])/1e9,3) if details and details["mtime_ns"] is not None else None,
                    "tapes":[r["barcode"] for r in (archived or verified_matches)] if (archived or verified_matches) else
                        [r["barcode"] for r in (same_size or matches)]})
    samples.sort(key=lambda r:(r["status"],r["path"].casefold()))
    return {"master_path":master,"counts":counts,"total":scanned,"files":samples,
            "truncated":scanned>len(samples),"comparison":"size_and_mtime_2s_tolerance",
            "note":"Metadata comparison only; no content SHA-256 verification"}
