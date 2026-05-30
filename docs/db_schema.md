# Chunks DB Schema

SQLite database produced by `scan_chunks.py`. Two tables: one per chunk, one per region file scan.

Default file: `/home/admin/claude/chunks_full.db`.

---

## Table: `chunks`

One row per chunk that exists in any region file. Chunks not generated at all are absent from this table — use the `regions` table to know which regions were scanned.

| column | type | nullable | description |
|---|---|---|---|
| `dim` | TEXT | no | `overworld`, `nether`, or `end`. Detected from the `.mca` path (`DIM-1` → nether, `DIM1` → end). |
| `cx` | INT | no | Absolute chunk X. Each chunk covers 16 blocks; block_x = cx*16. |
| `cz` | INT | no | Absolute chunk Z. block_z = cz*16. |
| `rx` | INT | no | Region X = `cx >> 5` (i.e. `cx // 32`). Stored explicitly for fast `GROUP BY` rollups. |
| `rz` | INT | no | Region Z = `cz >> 5`. |
| `inhabited_ticks` | INT | yes | Cumulative game ticks any player has been within sim distance of this chunk. 20 ticks = 1 second. NULL only when `error IS NOT NULL`. |
| `last_modified` | INT | no | Unix timestamp (seconds) from the region file's per-chunk timestamp slot — when the game last wrote this chunk. |
| `status` | TEXT | yes | Minecraft chunk generation status. `minecraft:full` = fully generated. Partial values (`minecraft:structure_starts`, `minecraft:biomes`, `minecraft:carvers`, `minecraft:initialize_light`) mean the game started generating but never finished — these chunks have no real content. NULL only on parse error. |
| `block_entities_count` | INT | yes | Number of tile entities (chests, furnaces, Create kinetic blocks, etc.). High counts in low-`inhabited_ticks` chunks suggest chunkloader-active automation. NULL only on parse error. |
| `error` | TEXT | yes | `NULL` if the chunk was parsed successfully. Otherwise a `"<ExceptionType>: <message>"` string. Other NBT-derived columns will be NULL when this is set. |

**Primary key:** `(dim, cx, cz)`

**Indexes:**
- PK index on `(dim, cx, cz)`
- `idx_chunks_region` on `(dim, rx, rz)` — for region-level aggregation

---

## Table: `regions`

One row per region file we've attempted to scan. This is the authoritative list of "what we've looked at" and drives the resume logic.

| column | type | nullable | description |
|---|---|---|---|
| `dim` | TEXT | no | Same values as `chunks.dim`. |
| `rx` | INT | no | Region X (from filename `r.<rx>.<rz>.mca`). |
| `rz` | INT | no | Region Z. |
| `path` | TEXT | no | Full filesystem path of the `.mca` file when scanned. |
| `file_size` | INT | yes | Size in bytes at scan time. 0 for `empty`. NULL if stat failed. |
| `file_mtime` | INT | yes | Unix mtime (seconds) at scan time. Used by resume: if disk mtime differs, region is rescanned. |
| `scanned_at` | INT | no | Unix time when the scan ran. |
| `scan_status` | TEXT | no | `ok` / `empty` / `error`. See below. |
| `error` | TEXT | yes | NULL when `scan_status='ok'`. Description for `empty` and `error`. |
| `chunks_present` | INT | yes | Number of chunks inserted into `chunks` for this region. NULL for non-ok scans. |
| `chunks_with_errors` | INT | yes | How many of those chunks had parse errors (per-chunk `error IS NOT NULL`). NULL for non-ok scans. |

**Primary key:** `(dim, rx, rz)`

**`scan_status` values:**
- `ok` — file parsed, chunks inserted into the `chunks` table.
- `empty` — file is 0 bytes (Minecraft stub from chunk generation that never persisted). No rows in `chunks`. Safe purge candidate.
- `error` — actual failure (I/O error, truncated header, etc.). `error` column has details. No chunks were inserted.

---

## Coordinate cheatsheet

- 1 region = 32 × 32 chunks
- 1 chunk = 16 × 16 blocks
- 1 region = 512 × 512 blocks
- `rx = cx >> 5`, `rz = cz >> 5` (right-shift handles negatives correctly)
- Local-chunk-in-region: `(cx & 31, cz & 31)` → `(0..31, 0..31)`
- Spawn region is `r.0.0.mca` → chunks `(0..31, 0..31)` → blocks `(0..511, 0..511)`

## Time conversion

- 1 game tick = 1/20 second
- `inhabited_ticks / 20` = seconds
- `inhabited_ticks / 72000` = hours
- Realistic ranges in this world:
  - 0 → never visited
  - 1–72000 (≤1h) → flyover / brief presence
  - >720000 (>10h) → base / hub
  - >7,200,000 (>100h) → chunkloader-active automation

---

## Useful queries

**Counts per dimension (sanity check):**
```sql
SELECT dim, scan_status, COUNT(*)
FROM regions GROUP BY dim, scan_status ORDER BY dim, scan_status;
```

**Region-level rollup (one row per region):**
```sql
SELECT dim, rx, rz,
       COUNT(*)                                                    AS chunks_present,
       SUM(CASE WHEN inhabited_ticks > 0 THEN 1 ELSE 0 END)        AS chunks_visited,
       MAX(inhabited_ticks)                                        AS max_inh,
       SUM(inhabited_ticks)                                        AS sum_inh,
       SUM(block_entities_count)                                   AS sum_be,
       SUM(CASE WHEN status = 'minecraft:full' THEN 1 ELSE 0 END)  AS chunks_full,
       SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END)          AS chunk_errors
FROM chunks
GROUP BY dim, rx, rz;
```

**Purge candidates — region files with no real player presence:**
```sql
SELECT r.dim, r.rx, r.rz, r.path, r.file_size, r.scan_status
FROM regions r
LEFT JOIN (
    SELECT dim, rx, rz, MAX(inhabited_ticks) AS max_inh
    FROM chunks GROUP BY dim, rx, rz
) c ON c.dim = r.dim AND c.rx = r.rx AND c.rz = r.rz
WHERE r.scan_status = 'empty'
   OR (r.scan_status = 'ok' AND COALESCE(c.max_inh, 0) = 0);
```

**All chunks in a specific region (e.g. for drill-down click):**
```sql
SELECT cx, cz, inhabited_ticks, status, block_entities_count, error
FROM chunks
WHERE dim = ? AND rx = ? AND rz = ?
ORDER BY cz, cx;
```

**Hot chunks across the world:**
```sql
SELECT dim, cx, cz, inhabited_ticks, status, block_entities_count
FROM chunks
WHERE inhabited_ticks > 0
ORDER BY inhabited_ticks DESC
LIMIT 50;
```

---

## Caveats and gotchas

- **NULL `inhabited_ticks`** means the chunk row couldn't be parsed — see `error`. Don't treat NULL as 0; exclude or distinguish in queries.
- **A chunk with `inhabited_ticks = 0` is still a real chunk**: generated, on disk, takes space. It just hasn't had a player nearby.
- **Region `scan_status = 'ok'` does not imply player activity**, just that we parsed it. Many `ok` regions have all-zero inhabited time (flyover-generated).
- **Region `scan_status = 'empty'` is good news**, not a failure. It means the file is 0 bytes and can be deleted with no loss.
- **`block_entities_count` is from modern (1.18+) chunk format**. The parser also handles legacy `Level.TileEntities` for older chunks, but this world is current-format.
- **Coordinates can be negative.** Use signed comparisons.
- **`status` may be NULL** for chunks where the NBT didn't include the field (rare, mostly partial-generation edge cases).
- **The scanner uses WAL mode.** When reading the DB from another tool, `.wal` and `.shm` sidecar files may exist; SQLite handles this automatically.
