#!/usr/bin/env python3
"""Drop unreadable chunks from a world's region files in place.

WorldUpgrader (--forceUpgrade) aborts an entire dimension if it hits a chunk it
can't read (e.g. the "bad length 0" / truncated-zlib chunks produced when a world
with disk-level EIO corruption is copied). This removes such chunks so the upgrade
can complete; the dropped chunks regenerate fresh in the new version.

A chunk is dropped if its length header is < 1, its compression byte is invalid,
or its payload fails to decompress. Region files are rewritten compacted. Runs
over region/, entities/ and poi/ for all three dimensions.
"""
import os, sys, struct, zlib, gzip, glob, argparse, multiprocessing

SECTOR = 4096
DIMS = {"overworld": "", "nether": "DIM-1", "end": "DIM1"}
FOLDERS = ("region", "entities", "poi")


def chunk_ok(buf):
    """True if the length-prefixed chunk payload in buf is readable."""
    if len(buf) < 5:
        return False
    length = struct.unpack(">i", buf[:4])[0]
    comp = buf[4]
    if length < 1:
        return False
    data = buf[5:5 + (length - 1)]
    if len(data) != length - 1:
        return False
    try:
        if comp == 1:
            gzip.decompress(data)
        elif comp == 2:
            zlib.decompress(data)
        elif comp == 3:
            pass  # uncompressed, already present
        elif comp == 4:
            return True  # LZ4 (no validator here); leave as-is
        else:
            return False
    except Exception:
        return False
    return True


def scrub_file(path):
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return (path, "stat_error", 0, 0)
    if size < 2 * SECTOR:
        return (path, "skip_small", 0, 0)
    with open(path, "rb") as f:
        loc = f.read(SECTOR)
        ts = f.read(SECTOR)
        present, keep = [], []
        for idx in range(1024):
            e = loc[idx * 4:idx * 4 + 4]
            off = (e[0] << 16) | (e[1] << 8) | e[2]
            cnt = e[3]
            if off >= 2 and cnt >= 1:
                present.append((idx, off, cnt))
        good = []
        for idx, off, cnt in present:
            f.seek(off * SECTOR)
            buf = f.read(cnt * SECTOR)
            if chunk_ok(buf):
                good.append((idx, buf))
        dropped = len(present) - len(good)
        if dropped == 0:
            return (path, "clean", len(present), 0)
        # rewrite compacted with only good chunks
        new_loc = bytearray(SECTOR)
        new_ts = bytearray(SECTOR)
        body = []
        cursor = 2
        for idx, buf in good:
            body.append(buf)
            new_loc[idx * 4]     = (cursor >> 16) & 0xFF
            new_loc[idx * 4 + 1] = (cursor >> 8) & 0xFF
            new_loc[idx * 4 + 2] = cursor & 0xFF
            new_loc[idx * 4 + 3] = len(buf) // SECTOR
            new_ts[idx * 4:idx * 4 + 4] = ts[idx * 4:idx * 4 + 4]
            cursor += len(buf) // SECTOR
    if not good:
        os.remove(path)
        return (path, "deleted_all_bad", len(present), dropped)
    tmp = path + ".scrubtmp"
    with open(tmp, "wb") as g:
        g.write(new_loc); g.write(new_ts)
        for b in body:
            g.write(b)
    os.replace(tmp, path)
    return (path, "scrubbed", len(present), dropped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    args = ap.parse_args()
    world = os.path.realpath(args.world)
    tasks = []
    for dim, sub in DIMS.items():
        root = world if sub == "" else os.path.join(world, sub)
        for folder in FOLDERS:
            tasks.extend(glob.glob(os.path.join(root, folder, "*.mca")))
    print(f"world: {world}\nfiles to check: {len(tasks)}   workers: {args.workers}")
    tot_dropped = tot_present = scrubbed = deleted = 0
    with multiprocessing.Pool(args.workers) as pool:
        for path, status, present, dropped in pool.imap_unordered(scrub_file, tasks, chunksize=8):
            tot_dropped += dropped
            tot_present += present
            if status == "scrubbed": scrubbed += 1
            elif status == "deleted_all_bad": deleted += 1
    print(f"\nchunks present: {tot_present:,}   bad chunks dropped: {tot_dropped:,}")
    print(f"files rewritten: {scrubbed}   files deleted (all bad): {deleted}")


if __name__ == "__main__":
    main()
