#!/usr/bin/env python3
"""Prune a Minecraft world in place using a chunks DB from scan_chunks.py.

Removes chunks whose inhabited time is below a threshold, and deletes region
files where every chunk is below it. Operates on region/, entities/ and poi/
for all three dimensions, keyed off the per-chunk InhabitedTime recorded in the
DB (entities/ and poi/ mirror the region/ keep-set).

Destructive and in place — point it at a COPY. It refuses to run against the
known live world path unless --allow-live is given. The DB is the keep oracle;
no rescanning. Chunks present on disk but absent from the DB (e.g. generated
after the scan) and chunks the DB couldn't parse (NULL inhabited) are KEPT.

Parallel: each .mca file is processed independently in a worker process.
"""
import os, sys, re, time, argparse, sqlite3, multiprocessing
from pathlib import Path

REGION_RE = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca$")
LIVE_WORLD = "/home/admin/ffcreate/world"   # never prune this unless --allow-live
SECTOR = 4096


def _retry_io(fn, *args, attempts=10, base=0.05):
    """Run a destructive fs op, retrying on transient Windows sharing locks.

    On Windows, antivirus (Defender) and the search indexer briefly open files
    right after they're written/touched, so os.remove/os.replace can raise
    PermissionError (WinError 32). Retry with exponential backoff; re-raise if it
    never clears. A no-op on POSIX, where these locks don't occur."""
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(base * (2 ** i))

# Per-region bitmasks, keyed (dim, rx, rz). On fork (Linux/macOS) workers inherit
# these from main() via copy-on-write. On spawn (Windows) workers re-import this
# module, so they are seeded explicitly through the Pool initializer below.
KEEP = {}    # bit idx set => keep this chunk (inhabited >= threshold, or NULL)
KNOWN = {}   # bit idx set => DB has a row for this chunk
DRY_RUN = False


def init_worker(keep, known, dry_run):
    """Pool initializer: seed worker globals (needed under spawn, harmless on fork)."""
    global KEEP, KNOWN, DRY_RUN
    KEEP, KNOWN, DRY_RUN = keep, known, dry_run

DIMS = {
    "overworld": "",
    "nether": "DIM-1",
    "end": "DIM1",
}
FOLDERS = ("region", "entities", "poi")


def _dilate(seeds, r):
    """Manhattan-disk dilation of a set of (cx, cz) chunk coords by radius r.

    Returns every chunk within Manhattan distance r (|dx|+|dz| <= r) of any
    seed, seeds included. Implemented as r rounds of 4-neighbour frontier
    growth, so work is ~4x the final set size (not seeds * diamond-area).
    Operates in GLOBAL chunk coords so the disk crosses region boundaries."""
    dilated = set(seeds)
    frontier = set(seeds)
    for _ in range(r):
        nxt = set()
        for cx, cz in frontier:
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                c = (cx + dx, cz + dz)
                if c not in dilated:
                    dilated.add(c)
                    nxt.add(c)
        frontier = nxt
    return dilated


def build_masks(db_path, threshold, dilate=0):
    """Build per-region keep/known bitmasks from the chunk DB.

    A chunk is KEPT if its inhabited time is >= threshold, if the DB couldn't
    read it (NULL), or — when dilate > 0 — if it lies within Manhattan distance
    `dilate` chunks of an inhabited (>= threshold) chunk. Dilation fills holes
    inside inhabited regions and feathers their edges so pruned chunks never
    strand a hole in the middle of built-up terrain."""
    known = {}
    seeds = {}      # dim -> set of inhabited (cx, cz)  (>= threshold)
    null_keep = {}  # dim -> set of unreadable (cx, cz) (NULL: always kept)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = db.execute("SELECT dim, cx, cz, inhabited_ticks FROM chunks")
    n = 0
    for dim, cx, cz, inh in cur:
        rx, rz = cx >> 5, cz >> 5
        idx = (cx & 31) + (cz & 31) * 32
        known[(dim, rx, rz)] = known.get((dim, rx, rz), 0) | (1 << idx)
        if inh is None:
            null_keep.setdefault(dim, set()).add((cx, cz))
        elif inh >= threshold:
            seeds.setdefault(dim, set()).add((cx, cz))
        n += 1
    db.close()

    keep = {}
    for dim in set(seeds) | set(null_keep):
        kept = _dilate(seeds.get(dim, set()), dilate) if dilate > 0 else set(seeds.get(dim, set()))
        # NULL chunks are kept as-is but are NOT dilation seeds.
        kept |= null_keep.get(dim, set())
        for cx, cz in kept:
            rx, rz = cx >> 5, cz >> 5
            idx = (cx & 31) + (cz & 31) * 32
            keep[(dim, rx, rz)] = keep.get((dim, rx, rz), 0) | (1 << idx)
    return keep, known, n


def prune_file(task):
    """Worker: prune one .mca file in place. Returns a stats dict."""
    path, dim, rx, rz = task
    base = {"dim": dim, "folder": os.path.basename(os.path.dirname(path))}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return {**base, "action": "error", "error": f"stat: {e}", "removed": 0, "kept": 0,
                "before": 0, "after": 0}

    # 0-byte stub: no chunks at all -> delete.
    if size == 0:
        if not DRY_RUN:
            _retry_io(os.remove, path)
        return {**base, "action": "deleted_stub", "removed": 0, "kept": 0, "before": 0, "after": 0}

    known = KNOWN.get((dim, rx, rz))
    keepm = KEEP.get((dim, rx, rz), 0)

    try:
        # All reads happen inside this `with`; the file handle is then closed
        # before any os.remove/os.replace. On Windows you cannot delete or replace
        # a file your own process still holds open (no FILE_SHARE_DELETE), so the
        # destructive ops must run only after the handle is released.
        with open(path, "rb") as f:
            header = f.read(2 * SECTOR)
            if len(header) < 2 * SECTOR:
                return {**base, "action": "error", "error": "truncated header",
                        "removed": 0, "kept": 0, "before": size, "after": size}
            loc = header[:SECTOR]
            ts = header[SECTOR:2 * SECTOR]

            present = []  # (idx, offset, sectors)
            for idx in range(1024):
                e = loc[idx * 4:idx * 4 + 4]
                off = (e[0] << 16) | (e[1] << 8) | e[2]
                cnt = e[3]
                if off >= 2 and cnt >= 1:
                    present.append((idx, off, cnt))

            if not present:
                # Empty of chunks -> delete, but only after the handle is closed.
                delete_after = True
                result_no_chunks = {**base, "action": "deleted_no_chunks", "removed": 0,
                                    "kept": 0, "before": size, "after": 0}
            else:
                delete_after = False
                result_no_chunks = None

            if not delete_after:
                # Keep predicate. If the DB never saw this region, keep everything
                # present (we can't judge it). Otherwise drop chunks the DB knows
                # and that are below threshold.
                if known is None:
                    keep_entries = present
                    unknown_region = True
                else:
                    unknown_region = False
                    keep_entries = [
                        (idx, off, cnt) for (idx, off, cnt) in present
                        if ((keepm >> idx) & 1) or not ((known >> idx) & 1)
                    ]

                if not keep_entries:
                    result_all_below = {**base, "action": "deleted_all_below",
                                        "removed": len(present), "kept": 0,
                                        "before": size, "after": 0}
                else:
                    result_all_below = None

                if keep_entries and len(keep_entries) == len(present):
                    # Nothing to drop — leave the file untouched.
                    act = "kept_unknown_region" if unknown_region else "kept_whole"
                    return {**base, "action": act, "removed": 0, "kept": len(present),
                            "before": size, "after": size}

                if keep_entries:
                    # Rewrite compacted: header (2 sectors) + kept chunk sectors verbatim.
                    new_loc = bytearray(SECTOR)
                    new_ts = bytearray(SECTOR)
                    chunks = []
                    cursor = 2
                    for (idx, off, cnt) in keep_entries:
                        f.seek(off * SECTOR)
                        # Guard: detect oversized/external (.mcc) chunk stubs so we
                        # never silently corrupt one. (None exist in this world.)
                        head5 = f.read(5)
                        if len(head5) == 5:
                            comp = head5[4]
                            if comp & 0x80:
                                return {**base, "action": "error",
                                        "error": f"external .mcc chunk at idx {idx}; not handled",
                                        "removed": 0, "kept": 0, "before": size, "after": size}
                        f.seek(off * SECTOR)
                        data = f.read(cnt * SECTOR)
                        if len(data) < cnt * SECTOR:
                            data += b"\x00" * (cnt * SECTOR - len(data))
                        chunks.append(data)
                        new_loc[idx * 4]     = (cursor >> 16) & 0xFF
                        new_loc[idx * 4 + 1] = (cursor >> 8) & 0xFF
                        new_loc[idx * 4 + 2] = cursor & 0xFF
                        new_loc[idx * 4 + 3] = cnt
                        new_ts[idx * 4:idx * 4 + 4] = ts[idx * 4:idx * 4 + 4]
                        cursor += cnt

        # ---- file handle now closed; safe to mutate on disk ----
        if delete_after:
            if not DRY_RUN:
                _retry_io(os.remove, path)
            return result_no_chunks
        if result_all_below is not None:
            if not DRY_RUN:
                _retry_io(os.remove, path)
            return result_all_below

        after = (2 + (cursor - 2)) * SECTOR
        if not DRY_RUN:
            tmp = path + ".prunetmp"
            with open(tmp, "wb") as g:
                g.write(new_loc)
                g.write(new_ts)
                for d in chunks:
                    g.write(d)
            _retry_io(os.replace, tmp, path)

        return {**base, "action": "rewritten", "removed": len(present) - len(keep_entries),
                "kept": len(keep_entries), "before": size, "after": after}

    except Exception as e:
        return {**base, "action": "error", "error": f"{type(e).__name__}: {e}",
                "removed": 0, "kept": 0, "before": size, "after": size}


def collect_tasks(world):
    tasks = []
    for dim, sub in DIMS.items():
        dimroot = world if sub == "" else os.path.join(world, sub)
        for folder in FOLDERS:
            d = os.path.join(dimroot, folder)
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                m = REGION_RE.search(name)
                if not m:
                    continue
                rx, rz = int(m.group(1)), int(m.group(2))
                tasks.append((os.path.join(d, name), dim, rx, rz))
    return tasks


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, help="world directory to prune (in place)")
    ap.add_argument("--db", default="/home/admin/claude/chunks_full.db")
    ap.add_argument("--min-ticks", type=int, default=1200,
                    help="keep chunks with inhabited_ticks >= this (default 1200 = 1 min)")
    ap.add_argument("--dilate", type=int, default=0,
                    help="also keep chunks within this Manhattan distance (in chunks) "
                         "of an inhabited chunk, to fill holes (default 0 = off)")
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-live", action="store_true",
                    help="permit running against the live world path (dangerous)")
    args = ap.parse_args()

    world = os.path.realpath(args.world)
    if not os.path.isdir(world):
        sys.exit(f"world dir not found: {world}")
    if world == os.path.realpath(LIVE_WORLD) and not args.allow_live:
        sys.exit(f"refusing to prune the live world {world!r} (use --allow-live to override)")
    if not os.path.exists(args.db):
        sys.exit(f"db not found: {args.db}")

    global KEEP, KNOWN, DRY_RUN
    DRY_RUN = args.dry_run

    t0 = time.time()
    print(f"world: {world}")
    print(f"db:    {args.db}")
    print(f"keep threshold: inhabited_ticks >= {args.min_ticks} "
          f"({args.min_ticks/1200:.0f} min)"
          f"{f'   + dilate {args.dilate} chunks (Manhattan)' if args.dilate else ''}"
          f"   {'DRY-RUN' if DRY_RUN else 'WRITE'}")
    KEEP, KNOWN, nrows = build_masks(args.db, args.min_ticks, args.dilate)
    print(f"loaded masks from {nrows:,} chunk rows across {len(KNOWN):,} known regions "
          f"({time.time()-t0:.1f}s)")

    tasks = collect_tasks(world)
    print(f"region/entities/poi .mca files to process: {len(tasks):,}   workers: {args.workers}\n")

    # Aggregate stats per dimension and per action.
    from collections import defaultdict
    agg = defaultdict(lambda: defaultdict(int))   # dim -> counters
    actions = defaultdict(int)
    errors = []
    t1 = time.time()
    done = 0
    with multiprocessing.Pool(args.workers, initializer=init_worker,
                              initargs=(KEEP, KNOWN, DRY_RUN)) as pool:
        for r in pool.imap_unordered(prune_file, tasks, chunksize=16):
            d = agg[r["dim"]]
            d["files"] += 1
            d["removed"] += r["removed"]
            d["kept"] += r["kept"]
            d["before"] += r["before"]
            d["after"] += r["after"]
            actions[r["action"]] += 1
            if r["action"] == "error":
                if len(errors) < 50:
                    errors.append(r.get("error", "?"))
            done += 1
            if done % 2000 == 0:
                rate = done / (time.time() - t1)
                print(f"  [{done:,}/{len(tasks):,}] {rate:,.0f} files/s")

    elapsed = time.time() - t1
    print(f"\ndone in {elapsed:.1f}s ({len(tasks)/elapsed:,.0f} files/s)\n")

    print(f"{'dim':10} {'files':>8} {'chunks_kept':>12} {'chunks_removed':>15} "
          f"{'size_before':>12} {'size_after':>12} {'freed':>12}")
    tb = ta = 0
    for dim in ("overworld", "nether", "end"):
        d = agg.get(dim)
        if not d:
            continue
        freed = d["before"] - d["after"]
        tb += d["before"]; ta += d["after"]
        print(f"{dim:10} {d['files']:>8,} {d['kept']:>12,} {d['removed']:>15,} "
              f"{human(d['before']):>12} {human(d['after']):>12} {human(freed):>12}")
    print(f"{'TOTAL':10} {sum(a['files'] for a in agg.values()):>8,} "
          f"{sum(a['kept'] for a in agg.values()):>12,} "
          f"{sum(a['removed'] for a in agg.values()):>15,} "
          f"{human(tb):>12} {human(ta):>12} {human(tb-ta):>12}")

    print("\nactions:")
    for a in sorted(actions):
        print(f"  {a:22} {actions[a]:>8,}")
    if errors:
        print(f"\n!! {actions['error']} errors (first {len(errors)}):")
        for e in errors:
            print(f"   {e}")
    if DRY_RUN:
        print("\n(DRY-RUN — nothing was modified)")


if __name__ == "__main__":
    main()
