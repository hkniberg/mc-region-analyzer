#!/usr/bin/env python3
"""Scan Minecraft region files and store per-chunk metadata in SQLite.

Parallel: worker processes parse region files concurrently; the main process
is the only writer to SQLite (no lock contention).

Resumable: on rerun, skips regions already scanned successfully whose .mca
mtime hasn't changed. Use --force to rescan everything.
"""
import sys, os, re, struct, zlib, gzip, sqlite3, io, time, argparse, multiprocessing
from pathlib import Path

REGION_RE = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    dim TEXT NOT NULL,
    cx INTEGER NOT NULL,
    cz INTEGER NOT NULL,
    rx INTEGER NOT NULL,
    rz INTEGER NOT NULL,
    inhabited_ticks INTEGER,
    last_modified INTEGER NOT NULL,
    status TEXT,
    block_entities_count INTEGER,
    error TEXT,
    PRIMARY KEY (dim, cx, cz)
);
CREATE INDEX IF NOT EXISTS idx_chunks_region ON chunks (dim, rx, rz);

CREATE TABLE IF NOT EXISTS regions (
    dim TEXT NOT NULL,
    rx INTEGER NOT NULL,
    rz INTEGER NOT NULL,
    path TEXT NOT NULL,
    file_size INTEGER,
    file_mtime INTEGER,
    scanned_at INTEGER NOT NULL,
    scan_status TEXT NOT NULL,
    error TEXT,
    chunks_present INTEGER,
    chunks_with_errors INTEGER,
    PRIMARY KEY (dim, rx, rz)
);
"""


def parse_chunk_root(data):
    """Walk the chunk NBT root and pull (inhabited_ticks, status, block_entities_count)."""
    buf = io.BytesIO(data)

    def read(n):
        b = buf.read(n)
        if len(b) != n:
            raise EOFError(f"need {n} got {len(b)}")
        return b

    def read_string():
        (n,) = struct.unpack(">H", read(2))
        return read(n).decode("utf-8", errors="replace")

    def skip_payload(tag):
        if   tag == 1: read(1)
        elif tag == 2: read(2)
        elif tag == 3: read(4)
        elif tag == 4: read(8)
        elif tag == 5: read(4)
        elif tag == 6: read(8)
        elif tag == 7:
            (n,) = struct.unpack(">i", read(4)); read(n)
        elif tag == 8:
            (n,) = struct.unpack(">H", read(2)); read(n)
        elif tag == 9:
            (ltag,) = struct.unpack(">b", read(1))
            (n,) = struct.unpack(">i", read(4))
            for _ in range(n):
                skip_payload(ltag)
        elif tag == 10:
            while True:
                (t,) = struct.unpack(">b", read(1))
                if t == 0: break
                read_string()
                skip_payload(t)
        elif tag == 11:
            (n,) = struct.unpack(">i", read(4)); read(n * 4)
        elif tag == 12:
            (n,) = struct.unpack(">i", read(4)); read(n * 8)
        else:
            raise ValueError(f"unknown nbt tag {tag}")

    def harvest_compound(into):
        while True:
            (t,) = struct.unpack(">b", read(1))
            if t == 0: return
            name = read_string()
            if t == 4 and name == "InhabitedTime":
                (v,) = struct.unpack(">q", read(8)); into["inhabited"] = v
            elif t == 8 and name in ("Status", "status"):
                (n,) = struct.unpack(">H", read(2))
                into["status"] = read(n).decode("utf-8", errors="replace")
            elif t == 9 and name in ("block_entities", "TileEntities"):
                (ltag,) = struct.unpack(">b", read(1))
                (n,) = struct.unpack(">i", read(4))
                into["be_count"] = n
                for _ in range(n):
                    skip_payload(ltag)
            elif t == 10 and name == "Level":
                harvest_compound(into)
            else:
                skip_payload(t)

    (root_tag,) = struct.unpack(">b", read(1))
    if root_tag != 10:
        raise ValueError(f"chunk root is not compound, tag={root_tag}")
    read_string()
    out = {"inhabited": 0, "status": None, "be_count": 0}
    harvest_compound(out)
    return out["inhabited"], out["status"], out["be_count"]


def iter_region_chunks(path, dim, rx, rz):
    """Yield per-chunk dicts for one region file."""
    with open(path, "rb") as f:
        locations = f.read(4096)
        timestamps = f.read(4096)
        if len(locations) < 4096 or len(timestamps) < 4096:
            raise IOError(f"region header truncated ({len(locations)}+{len(timestamps)} bytes)")
        for idx in range(1024):
            loc = locations[idx*4:idx*4+4]
            offset = int.from_bytes(loc[:3], "big")
            if offset == 0:
                continue
            last_modified = int.from_bytes(timestamps[idx*4:idx*4+4], "big")
            cx = rx * 32 + (idx % 32)
            cz = rz * 32 + (idx // 32)
            err = None
            inhabited = status = be_count = None
            try:
                f.seek(offset * 4096)
                hdr = f.read(5)
                if len(hdr) < 5:
                    raise IOError("chunk header truncated")
                (length,) = struct.unpack(">i", hdr[:4])
                compression = hdr[4]
                if length < 1:
                    raise ValueError(f"bad chunk length {length}")
                raw = f.read(length - 1)
                if len(raw) != length - 1:
                    raise IOError(f"chunk payload truncated ({len(raw)}/{length-1})")
                if compression == 1:
                    data = gzip.decompress(raw)
                elif compression == 2:
                    data = zlib.decompress(raw)
                elif compression == 3:
                    data = raw
                else:
                    raise ValueError(f"unsupported compression {compression}")
                inhabited, status, be_count = parse_chunk_root(data)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            yield {
                "dim": dim, "cx": cx, "cz": cz, "rx": rx, "rz": rz,
                "inhabited_ticks": inhabited,
                "last_modified": last_modified,
                "status": status,
                "block_entities_count": be_count,
                "error": err,
            }


def detect_dim_from_path(path):
    s = str(path)
    if "/DIM-1/" in s: return "nether"
    if "/DIM1/" in s:  return "end"
    return "overworld"


def process_region(task):
    """Worker entrypoint. Pure function: stat, parse, return result dict."""
    path, dim = task
    m = REGION_RE.search(os.path.basename(path))
    if not m:
        return {"path": path, "dim": dim, "rx": None, "rz": None,
                "outcome": "bad_name", "error": "not a region filename",
                "file_size": None, "file_mtime": None, "rows": []}
    rx, rz = int(m.group(1)), int(m.group(2))

    try:
        st = os.stat(path)
    except OSError as e:
        return {"path": path, "dim": dim, "rx": rx, "rz": rz,
                "outcome": "stat_error", "error": f"stat failed: {e}",
                "file_size": None, "file_mtime": None, "rows": []}

    file_size = st.st_size
    file_mtime = int(st.st_mtime)

    if file_size == 0:
        return {"path": path, "dim": dim, "rx": rx, "rz": rz,
                "outcome": "empty", "error": "region file is 0 bytes (Minecraft stub, no chunk data)",
                "file_size": file_size, "file_mtime": file_mtime, "rows": []}

    try:
        rows = list(iter_region_chunks(path, dim, rx, rz))
    except Exception as e:
        return {"path": path, "dim": dim, "rx": rx, "rz": rz,
                "outcome": "region_error", "error": f"{type(e).__name__}: {e}",
                "file_size": file_size, "file_mtime": file_mtime, "rows": []}

    return {"path": path, "dim": dim, "rx": rx, "rz": rz,
            "outcome": "ok", "error": None,
            "file_size": file_size, "file_mtime": file_mtime, "rows": rows}


def apply_result(db, result):
    """Write one worker result (region + its chunks) in a single transaction."""
    dim, rx, rz = result["dim"], result["rx"], result["rz"]
    path = result["path"]
    file_size = result["file_size"]
    file_mtime = result["file_mtime"]
    now = int(time.time())

    if result["outcome"] != "ok":
        status = "empty" if result["outcome"] == "empty" else "error"
        db.execute("""
            INSERT OR REPLACE INTO regions
              (dim, rx, rz, path, file_size, file_mtime, scanned_at, scan_status, error, chunks_present, chunks_with_errors)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """, (dim, rx, rz, path, file_size, file_mtime, now, status, result["error"]))
        db.commit()
        return

    rows = result["rows"]
    db.execute("BEGIN")
    db.execute("DELETE FROM chunks WHERE dim=? AND rx=? AND rz=?", (dim, rx, rz))
    if rows:
        db.executemany("""
            INSERT INTO chunks
              (dim, cx, cz, rx, rz, inhabited_ticks, last_modified, status, block_entities_count, error)
            VALUES
              (:dim, :cx, :cz, :rx, :rz, :inhabited_ticks, :last_modified, :status, :block_entities_count, :error)
        """, rows)
    chunks_present = len(rows)
    chunks_with_errors = sum(1 for r in rows if r["error"] is not None)
    db.execute("""
        INSERT OR REPLACE INTO regions
          (dim, rx, rz, path, file_size, file_mtime, scanned_at, scan_status, error, chunks_present, chunks_with_errors)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ok', NULL, ?, ?)
    """, (dim, rx, rz, path, file_size, file_mtime, now, chunks_present, chunks_with_errors))
    db.commit()


def build_skip_set(db, paths_with_dims):
    """Return paths that should be skipped (already scanned OK and mtime matches)."""
    skip = set()
    # Cache existing region rows
    existing = {}
    for dim, rx, rz, st, mt in db.execute(
        "SELECT dim, rx, rz, scan_status, file_mtime FROM regions"
    ):
        existing[(dim, rx, rz)] = (st, mt)
    for path, dim in paths_with_dims:
        m = REGION_RE.search(os.path.basename(path))
        if not m:
            continue
        rx, rz = int(m.group(1)), int(m.group(2))
        prev = existing.get((dim, rx, rz))
        if not prev:
            continue
        prev_status, prev_mtime = prev
        if prev_status not in ("ok", "empty"):
            continue
        try:
            cur_mtime = int(os.stat(path).st_mtime)
        except OSError:
            continue
        if cur_mtime == prev_mtime:
            skip.add(path)
    return skip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--force", action="store_true", help="Rescan everything")
    ap.add_argument("--dim", help="Override dimension (default: detect from path)")
    ap.add_argument("--workers", type=int, default=min(16, multiprocessing.cpu_count()),
                    help="Number of worker processes (default: min(16, cpu_count))")
    ap.add_argument("--minx", type=int)
    ap.add_argument("--maxx", type=int)
    ap.add_argument("--minz", type=int)
    ap.add_argument("--maxz", type=int)
    ap.add_argument("--progress-every", type=int, default=50,
                    help="Print a progress line every N regions (default 50)")
    ap.add_argument("regions", nargs="+")
    args = ap.parse_args()

    def in_range(path):
        m = REGION_RE.search(os.path.basename(path))
        if not m:
            return False
        rx, rz = int(m.group(1)), int(m.group(2))
        if args.minx is not None and rx < args.minx: return False
        if args.maxx is not None and rx > args.maxx: return False
        if args.minz is not None and rz < args.minz: return False
        if args.maxz is not None and rz > args.maxz: return False
        return True

    region_files = []
    for p in args.regions:
        pp = Path(p)
        if pp.is_dir():
            region_files.extend(sorted(str(x) for x in pp.glob("r.*.mca") if in_range(str(x))))
        elif in_range(p):
            region_files.append(p)

    if not region_files:
        print("no region files match filters", file=sys.stderr)
        return

    paths_with_dims = [(p, args.dim or detect_dim_from_path(p)) for p in region_files]

    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")

    if args.force:
        skip = set()
    else:
        skip = build_skip_set(db, paths_with_dims)

    work_items = [(p, d) for p, d in paths_with_dims if p not in skip]

    print(f"regions matched: {len(paths_with_dims)}   skipped (already scanned): {len(skip)}   to scan: {len(work_items)}   workers: {args.workers}")
    if not work_items:
        db.close()
        return

    start = time.time()
    counts = {"ok": 0, "region_error": 0, "stat_error": 0, "bad_name": 0}

    if args.workers <= 1:
        # Serial path (no Pool overhead for tiny runs)
        for i, task in enumerate(work_items, 1):
            result = process_region(task)
            apply_result(db, result)
            counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
            if i % args.progress_every == 0 or i == len(work_items):
                rate = i / (time.time() - start)
                eta = (len(work_items) - i) / rate if rate > 0 else 0
                print(f"  [{i}/{len(work_items)}] {rate:.1f} reg/s, eta {eta:.0f}s")
    else:
        with multiprocessing.Pool(args.workers) as pool:
            for i, result in enumerate(pool.imap_unordered(process_region, work_items, chunksize=4), 1):
                apply_result(db, result)
                counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
                if i % args.progress_every == 0 or i == len(work_items):
                    rate = i / (time.time() - start)
                    eta = (len(work_items) - i) / rate if rate > 0 else 0
                    print(f"  [{i}/{len(work_items)}] {rate:.1f} reg/s, eta {eta:.0f}s")

    db.close()
    elapsed = time.time() - start
    err_total = counts.get('region_error',0) + counts.get('stat_error',0) + counts.get('bad_name',0)
    print(f"\ndone in {elapsed:.1f}s: ok={counts.get('ok',0)}  empty={counts.get('empty',0)}  errors={err_total}")
    print(f"  sqlite: {args.db}")


if __name__ == "__main__":
    main()
