#!/usr/bin/env python3
"""Read InhabitedTime from one chunk in an Anvil region file."""
import sys, struct, zlib, gzip, io

def read_nbt_find_long(data, target):
    """Walk an NBT compound stream and return the first Long tag named `target`."""
    buf = io.BytesIO(data)

    def read(n):
        b = buf.read(n)
        if len(b) != n:
            raise EOFError
        return b

    def read_name():
        (nlen,) = struct.unpack(">H", read(2))
        return read(nlen).decode("utf-8")

    def skip_payload(tag):
        if tag == 1: read(1)
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
            walk_compound()
        elif tag == 11:
            (n,) = struct.unpack(">i", read(4)); read(n * 4)
        elif tag == 12:
            (n,) = struct.unpack(">i", read(4)); read(n * 8)
        else:
            raise ValueError(f"unknown tag {tag}")

    found = []

    def walk_compound():
        while True:
            (tag,) = struct.unpack(">b", read(1))
            if tag == 0:
                return
            name = read_name()
            if tag == 4 and name == target:
                (v,) = struct.unpack(">q", read(8))
                found.append(v)
            else:
                skip_payload(tag)

    # root: 1 byte tag (compound=10), 2 byte name length, name, then compound payload
    (root_tag,) = struct.unpack(">b", read(1))
    assert root_tag == 10
    read_name()  # root name (usually "")
    walk_compound()
    return found[0] if found else None


def read_chunk(region_path, cx, cz):
    with open(region_path, "rb") as f:
        idx = (cx % 32) + (cz % 32) * 32
        f.seek(idx * 4)
        loc = f.read(4)
        offset = int.from_bytes(loc[:3], "big")
        sectors = loc[3]
        if offset == 0:
            return None
        f.seek(offset * 4096)
        (length,) = struct.unpack(">i", f.read(4))
        (compression,) = struct.unpack(">b", f.read(1))
        raw = f.read(length - 1)
        if compression == 1:
            data = gzip.decompress(raw)
        elif compression == 2:
            data = zlib.decompress(raw)
        elif compression == 3:
            data = raw
        else:
            raise ValueError(f"unsupported compression {compression}")
        return data


def scan_region(region_path):
    """Yield (cx, cz, inhabited_ticks) for every present chunk in the region."""
    # Region coords from filename r.X.Z.mca
    import os, re
    m = re.match(r"r\.(-?\d+)\.(-?\d+)\.mca$", os.path.basename(region_path))
    if not m:
        raise ValueError(f"unexpected region filename: {region_path}")
    rx, rz = int(m.group(1)), int(m.group(2))
    with open(region_path, "rb") as f:
        header = f.read(4096)
    for idx in range(1024):
        loc = header[idx * 4:idx * 4 + 4]
        offset = int.from_bytes(loc[:3], "big")
        if offset == 0:
            continue
        local_x = idx % 32
        local_z = idx // 32
        cx = rx * 32 + local_x
        cz = rz * 32 + local_z
        try:
            data = read_chunk(region_path, cx, cz)
            if data is None:
                continue
            v = read_nbt_find_long(data, "InhabitedTime")
            yield cx, cz, v if v is not None else 0
        except Exception as e:
            print(f"  ! chunk {cx},{cz}: {e}", file=sys.stderr)


def fmt_ticks(t):
    s = t / 20
    return f"{t} ticks ({s:.1f}s, {s/60:.1f}m, {s/3600:.2f}h)"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage:")
        print("  inhabited.py <region.mca>                 # scan all chunks in region")
        print("  inhabited.py <region.mca> <cx> <cz>       # single chunk")
        sys.exit(1)

    region = sys.argv[1]

    if len(sys.argv) >= 4:
        cx = int(sys.argv[2])
        cz = int(sys.argv[3])
        data = read_chunk(region, cx, cz)
        if data is None:
            print(f"chunk {cx},{cz} not present in {region}")
            sys.exit(0)
        v = read_nbt_find_long(data, "InhabitedTime")
        if v is None:
            print(f"chunk {cx},{cz}: no InhabitedTime tag found ({len(data)} bytes NBT)")
        else:
            print(f"chunk {cx},{cz}: InhabitedTime = {fmt_ticks(v)}")
        sys.exit(0)

    # Scan whole region
    results = list(scan_region(region))
    if not results:
        print(f"{region}: no chunks present")
        sys.exit(0)

    times = [t for _, _, t in results]
    present = len(times)
    visited = sum(1 for t in times if t > 0)
    total_ticks = sum(times)
    max_t = max(times)
    max_chunk = next((cx, cz) for cx, cz, t in results if t == max_t)

    print(f"region: {region}")
    print(f"  chunks present:  {present} / 1024")
    print(f"  chunks visited:  {visited} (InhabitedTime > 0)")
    print(f"  total inhabited: {fmt_ticks(total_ticks)}")
    print(f"  max chunk:       {max_chunk} = {fmt_ticks(max_t)}")
    print(f"  median:          {fmt_ticks(sorted(times)[len(times)//2])}")

    # Histogram by hour buckets
    buckets = [0, 0, 0, 0, 0, 0]  # 0, <1m, <10m, <1h, <10h, >=10h
    for t in times:
        s = t / 20
        if t == 0: buckets[0] += 1
        elif s < 60: buckets[1] += 1
        elif s < 600: buckets[2] += 1
        elif s < 3600: buckets[3] += 1
        elif s < 36000: buckets[4] += 1
        else: buckets[5] += 1
    labels = ["= 0", "< 1m", "< 10m", "< 1h", "< 10h", ">= 10h"]
    print("  distribution:")
    for lbl, n in zip(labels, buckets):
        bar = "#" * min(50, n)
        print(f"    {lbl:>6}: {n:4d}  {bar}")
