# FFCreate World Browser — Implementation Plan

A web app for visually assessing the FFCreate Minecraft world prior to purging unused regions during a migration. Read-only browser, no purge actions in v1.

## Context

The world we're analyzing is `/home/admin/ffcreate/world/`. It contains 24,964 region files across three dimensions:

- Overworld (`/region/`): 24,232 region files — but **20,589 are 0-byte Minecraft stubs**.
- Nether (`/DIM-1/region/`): 447 files, all real.
- End (`/DIM1/region/`): 285 files (281 real, 3 empty, 1 with a hardware-level I/O error on `r.0.7.mca`).

Of the 3,643 overworld region files with real data, **only ~140 have any chunk with meaningful player time** (>10h). The rest is flyover cruft. The migration assessment hinges on letting the user see this picture clearly.

A massive Create base lives around chunks `(-58, -125)` — chunkloader-driven, ~97,000 hours (≈11 years) per chunk. Regions `(-2,-5)` and `(-2,-4)` are the heart of it.

## Inputs that already exist

- **`/home/admin/claude/scan_chunks.py`** — the scanner. Parallel, resumable. Already ran across the whole world.
- **`/home/admin/claude/chunks_full.db`** — SQLite database with per-chunk and per-region tables. Columns documented in:
- **`/home/admin/claude/chunks_db_schema.md`** — schema reference. **Read this first** before writing queries. It covers tables, columns, NULL semantics, coordinate conventions, ticks→hours, and ready-to-copy queries.

## Decisions (locked in)

1. **Backend:** Python + Flask, reading directly from `chunks_full.db`. Open SQLite read-only.
2. **Frontend:** Single HTML page + vanilla JS + `<canvas>`. No build step, no framework.
3. **Port:** Bind `0.0.0.0:8089`. External URL: `http://192.168.1.245:8089`.
4. **No "mark for purge" UX in v1.** Assessment only. Purge action is a separate later step.
5. **Coordinate convention:** Minecraft — Z increases downward, X increases rightward.
6. **Read-only.** No mutations to the database from the web app.

## Three views

### View 1 — World map

Top-level. Big `<canvas>`, one cell per region.

- Default zoom: fit to the populated cluster near origin. Don't show the far-out outliers (e.g. `r.5859.-1`) at default zoom — they'd compress everything else to invisible. Provide a "fit all" toggle.
- Pan + zoom (mouse drag + scroll wheel).
- Coordinate readout follows the cursor: `rx=-2 rz=-5`.
- Hover tooltip: scan_status, chunks_present, max_inhabited (hours), sum_BEs.
- Click a region → drill into View 2.

### View 2 — Region drill-down

32×32 chunk grid for the selected region.

- Same color modes as View 1.
- Hover tooltip: chunk coords, inhabited_ticks (formatted as hours), status, BE count.
- Click a chunk → side panel (View 3 below).
- "Back to world" button. URL reflects state: `/region/overworld/-2/-5`.

### View 3 — Chunk inspector (side panel)

Shows all DB columns for the selected chunk, formatted:

- `inhabited_ticks` → "1,234,567 ticks (17.1 hours)"
- `last_modified` → ISO-style readable date
- block coordinates derived from chunk coords (`block_x = cx * 16`)
- generation status, BE count, error string if any

## Color modes (toggle in sidebar)

Ship at minimum the first three. Modes 4 and 5 are useful extras — if scope is tight, defer them.

| # | mode | encoding | purpose |
|---|---|---|---|
| 1 | **Status** *(default)* | green=visited, yellow=generated-but-unvisited, light gray=empty stub, dark gray=partial-gen-only, red=error | Categorical big-picture purge view. |
| 2 | **Inhabited time (log)** | white→deep blue on `max_inhabited_ticks`, log scale | Hot zones (bases) jump out; flyover stays light. |
| 3 | **Block entities (log)** | white→orange on summed BE count | Catches chunkloader-driven automation even with low inhabited time. |
| 4 | Last modified | white→purple on max `last_modified` per region | "Is this still in use?" |
| 5 | Chunk completeness | red→green on fraction of chunks with `status='minecraft:full'` | Region-of-cruft detection. |

Toggling is instant — same data, repaint only.

## Interaction model

- **Hover:** tooltip with quick stats.
- **Click region:** drill in (View 2).
- **Click chunk:** open inspector (View 3).
- **Sidebar filters:** dimension toggle (Overworld/Nether/End), min-inhabited threshold slider, "hide empty regions" toggle.
- **Running counters** in a corner: "X regions visible, Y MB total file size, Z match filter."

## Layout

```
+-------------------+--------------------------------+----------+
| Sidebar           | Canvas (world or region grid)  | Info     |
| - Dim: [OW][N][E] |                                | panel    |
| - Color mode:     |   ┌──────────────────────┐     | (hover/  |
|   ( ) status      |   │ . . . . . . . . . .  │     |  click)  |
|   ( ) inhabited   |   │ . . . ▓▓▓▓▓▓▓▓▓ . .  │     |          |
|   ( ) BE          |   │ . . ▓████████▓ . . . │     |          |
|   ( ) modified    |   │ . . ▓▓▓▓▓▓▓▓▓ . . .  │     |          |
|   ( ) complete    |   │ . . . . . . . . . .  │     |          |
| - Filters: ...    |   └──────────────────────┘     |          |
| - Stats: ...      |   coord readout: rx=-2 rz=-5   |          |
+-------------------+--------------------------------+----------+
```

## API endpoints

Three endpoints. JSON responses. SQLite opened read-only with `?mode=ro`.

### `GET /api/world?dim=overworld`

Returns the per-region rollup for one dimension. One object per region file, including empty/error ones.

```json
[
  {
    "rx": -2, "rz": -5,
    "scan_status": "ok",
    "chunks_present": 1024,
    "chunks_visited": 1024,
    "max_inh": 348909120,
    "sum_inh": 2434125789,
    "sum_be": 7314,
    "chunks_full": 1024,
    "max_modified": 1779685074,
    "file_size": 12345678
  },
  ...
]
```

Generate once on first request, cache in memory (the data is static during a session).

### `GET /api/region/<dim>/<rx>/<rz>`

Returns all chunks in a region.

```json
[
  { "cx": -64, "cz": -160, "inhabited_ticks": 6088963, "last_modified": 1779684270,
    "status": "minecraft:full", "block_entities_count": 34, "error": null },
  ...
]
```

### `GET /api/chunk/<dim>/<cx>/<cz>`

Returns one chunk. Convenience endpoint; same data as a filtered region call.

## File layout

Create the app in a new directory:

```
/home/admin/claude/chunks_viz/
├── app.py              # Flask app, all backend logic
├── static/
│   ├── index.html      # the single page
│   ├── app.js          # all frontend logic (no build step)
│   └── style.css
└── README.md           # how to run it
```

## Running it

The user is on the Docker VM. Run with:

```bash
cd /home/admin/claude/chunks_viz
python3 app.py
# or via Flask: FLASK_APP=app.py flask run --host 0.0.0.0 --port 8089
```

Should also work via systemd unit if it becomes a long-running tool, but v1 can be run-it-by-hand.

## Out of scope (v1)

- Purge actions / "mark for delete" UX.
- Block-level rendering (Overviewer already does that at `http://192.168.1.245:8088`).
- Authentication, multi-user.
- Persistent annotations ("this region is the iron farm").
- Writing to the SQLite database.
- Rescanning the world from the web UI.

## When picking this up cold

1. Read `chunks_db_schema.md` to understand the data model.
2. Verify `chunks_full.db` exists and is current. Quick sanity check: `python3 -c "import sqlite3; db=sqlite3.connect('chunks_full.db'); print(list(db.execute('SELECT scan_status, COUNT(*) FROM regions GROUP BY scan_status')))"` — expect `[('empty', 20592), ('error', 1), ('ok', 4371)]`.
3. Confirm port 8089 is free: `ss -ltn | grep ':8089'` → should be empty.
4. Build the Flask app per this plan.
5. Test by browsing `http://192.168.1.245:8089` from outside the Docker VM.
