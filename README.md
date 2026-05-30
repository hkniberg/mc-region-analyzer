# mc-region-analyzer

Scan a Minecraft world's region files into a SQLite database, then browse the
result in a web app to decide which regions are worth keeping and which can be
purged.

Built to prepare a large modded world (FFCreate) for migration to a fresh map:
most of a long-lived world is flyover terrain that was generated once and never
touched again. This tool makes that visible so you can delete it with confidence.

## What it captures

For every chunk in every `.mca` region file, the scanner records:

- **`inhabited_ticks`** — cumulative game ticks a player (or chunkloader-driven
  fake player) has been within simulation distance of the chunk. 20 ticks = 1
  second; 72,000 ticks = 1 hour. This is the primary "does anyone care about
  this chunk" signal. `0` = never visited.
- **`status`** — chunk generation status (`minecraft:full` vs partial).
- **`block_entities_count`** — chests, furnaces, Create kinetic blocks, etc.
  High counts with low inhabited time flag chunkloader-driven automation.
- **`last_modified`** — when the game last wrote the chunk.

It also records one row per region file (scanned / empty / error) so reruns are
resumable and corrupt files are tracked rather than silently skipped.

## Layout

```
scan_chunks.py          The scanner: region files -> SQLite. stdlib only.
prune_world.py          Prune a world in place using the DB: drop low-inhabited
                        chunks and dead region files. stdlib only.
inhabited.py            Tiny companion: dump InhabitedTime for one chunk/region.
viz/                    Web app for browsing the database.
  app.py                Flask backend (3 read-only JSON endpoints).
  static/               Single-page canvas UI (no build step).
data/
  chunks_sample.db      Small sample DB so the app runs out of the box.
docs/
  db_schema.md          Full database schema reference + ready-to-use queries.
  viz_design_notes.md   Design rationale for the web app.
```

## Requirements

- **Scanner:** Python 3 standard library only (no pip installs). Uses
  `zlib`/`gzip`/`struct` to parse the Anvil region format and NBT directly.
- **Web app:** Flask (`apt install python3-flask` or `pip install flask`).

## 1. Scan a world

The scanner takes one or more region directories (or individual `.mca` files).
Dimension is auto-detected from the path (`DIM-1` → nether, `DIM1` → end,
otherwise overworld).

```bash
python3 scan_chunks.py --db chunks_full.db \
    /path/to/world/region \
    /path/to/world/DIM-1/region \
    /path/to/world/DIM1/region
```

Runs in parallel (worker processes parse; the main process is the sole SQLite
writer, so there's no lock contention). A ~25,000-file world scans in a few
minutes on 16 workers.

**Useful flags:**

| flag | meaning |
|---|---|
| `--db PATH` | output database (required) |
| `--workers N` | worker processes (default `min(16, cpu_count)`) |
| `--force` | rescan everything (default: skip regions whose mtime is unchanged) |
| `--dim NAME` | override dimension detection |
| `--minx/--maxx/--minz/--maxz` | restrict to a region-coordinate box (handy for testing) |
| `--progress-every N` | progress line cadence (default 50) |

Reruns are **resumable**: a region already scanned `ok`/`empty` whose file mtime
hasn't changed is skipped. Interrupt and restart freely.

### Peek at a single chunk without a database

```bash
python3 inhabited.py /path/to/world/region/r.0.0.mca 5 5   # one chunk
python3 inhabited.py /path/to/world/region/r.0.0.mca       # whole region summary
```

## 2. Prune a world

Using a database from step 1, prune a world **in place** — drop chunks below an
inhabited-time threshold and delete region files where every chunk is below it.
Applies to `region/`, `entities/` and `poi/` across all three dimensions
(`entities/` and `poi/` mirror the `region/` keep-set). Region files are rewritten
compacted, so the freed space is real.

> **Point this at a COPY.** It's destructive and irreversible. It refuses to run
> against the hard-coded live world path unless `--allow-live` is given, and never
> writes to anything but `--world`.

```bash
# dry-run first — reports what would change, writes nothing
python3 prune_world.py --world /path/to/world-copy --dry-run

# then for real
python3 prune_world.py --world /path/to/world-copy
```

**Useful flags:**

| flag | meaning |
|---|---|
| `--world PATH` | world directory to prune in place (required) |
| `--db PATH` | chunks DB to use as the keep oracle (default `/home/admin/claude/chunks_full.db`) |
| `--min-ticks N` | keep chunks with `inhabited_ticks >= N` (default 1200 = 1 min) |
| `--workers N` | worker processes (default `os.cpu_count()`) |
| `--dry-run` | report only, change nothing |
| `--allow-live` | permit running against the live world path (dangerous) |

Safe-by-default keep rule: chunks present on disk but **absent from the DB** (e.g.
generated after the scan) and chunks the DB couldn't parse are **kept**, so a
stale DB never causes silent data loss.

> **Copy from a quiesced world.** Copying a world while its server is autosaving
> can capture region files mid-write (torn chunks that regenerate on load). Stop
> the server, or flush + pause saves (`save-off` / `save-all flush`) before copying.

## 3. Browse the database

```bash
cd viz
python3 app.py                              # serves data/chunks_sample.db
# or point at a full scan:
CHUNKS_DB=../chunks_full.db python3 app.py
```

Then open `http://<host>:8089`.

- **World map** — one cell per region. Pan (drag), zoom (wheel), hover for
  stats. Color modes: status, inhabited time (log), block entities (log), last
  modified, chunk completeness. "Fit to populated" frames the dense cluster;
  "Fit all" includes far-out exploration trails.
- **Region drill-down** — double-click a region (or click → "Drill into
  region") for its 32×32 chunk grid. `Esc` returns to the world map.
- **Chunk inspector** — click any chunk for its full row.
- **Filters** — dimension toggle, "min inhabited hours" slider (keeps regions
  whose busiest chunk clears the threshold), "hide empty regions".

The app opens the database read-only and never writes to it.

## Database schema

Two tables — `chunks` (one row per chunk) and `regions` (one row per region
file, tracking scan state). Full column docs, coordinate conventions, tick→hour
conversion, and copy-paste queries are in [`docs/db_schema.md`](docs/db_schema.md).

Quick orientation:

```sql
-- How many regions actually have meaningful player presence?
SELECT COUNT(DISTINCT dim || rx || ',' || rz)
FROM chunks WHERE inhabited_ticks > 72000;   -- > 1 hour

-- Purge candidates: region files with no real presence.
SELECT r.dim, r.rx, r.rz, r.path, r.file_size
FROM regions r
LEFT JOIN (SELECT dim, rx, rz, MAX(inhabited_ticks) m FROM chunks GROUP BY dim, rx, rz) c
  ON c.dim = r.dim AND c.rx = r.rx AND c.rz = r.rz
WHERE r.scan_status = 'empty' OR (r.scan_status = 'ok' AND COALESCE(c.m, 0) = 0);
```

## Notes

- A `0`-byte region file is a normal Minecraft stub (generation started but
  nothing persisted) — recorded as `scan_status = 'empty'`, a safe purge
  candidate, not an error.
- `inhabited_ticks` counts *simulation-distance* presence, not "player stood in
  this exact chunk", so a stationary player ticks up the whole area around them.
  Chunkloaders register as fake players, so automation-only chunks still show
  high values — useful for spotting farms.
