# Web App — Design Notes

Design rationale for the region browser in `viz/`. A read-only tool for visually
assessing a Minecraft world prior to purging unused regions during a migration.

## Why this exists

A long-lived world accumulates enormous amounts of terrain that was generated
once (by a player flying or running through it) and never touched again. When the
data is examined chunk by chunk, the picture is stark: in the world this tool was
built for, of ~24,000 overworld region files, ~20,000 were 0-byte stubs and only
~140 contained any chunk with meaningful player time. Everything else is flyover
cruft that can be deleted.

`inhabited_ticks` is the key signal. Chunkloader-driven automation shows up as
very high inhabited time (a fake player keeps the chunk simulated), so bases and
farms stand out clearly from terrain. The job of the app is to make that
distribution visible at a glance, then let you drill in to confirm.

## Decisions

1. **Backend:** Python + Flask, reading the SQLite DB read-only (`?mode=ro`).
2. **Frontend:** Single HTML page + vanilla JS + `<canvas>`. No build step, no framework.
3. **No "mark for purge" UX.** Assessment only; purging is a separate manual step.
4. **Coordinate convention:** Minecraft — Z increases downward, X increases rightward.
5. **Read-only.** The app never mutates the database.

## Three views

### View 1 — World map

Big `<canvas>`, one cell per region.

- Default zoom fits the populated cluster near origin. Far-out exploration trails
  (a single region tens of thousands away) would otherwise compress everything to
  invisible — a "fit all" toggle shows them when wanted.
- Pan (drag) + zoom (wheel).
- Coordinate readout follows the cursor (`rx=-2 rz=-5`).
- Hover tooltip: scan status, chunks present, max inhabited (hours), summed block entities.
- Click / double-click a region → drill into View 2.

### View 2 — Region drill-down

32×32 chunk grid for the selected region.

- Same color modes as View 1, applied per chunk.
- Hover tooltip: chunk coords, inhabited time (hours), status, block-entity count.
- Click a chunk → inspector (View 3). `Esc` returns to the world map.

### View 3 — Chunk inspector (side panel)

All DB columns for the selected chunk, formatted:

- `inhabited_ticks` → "1,234,567 ticks (17.1 hours)"
- `last_modified` → readable date
- block coordinates derived from chunk coords (`block_x = cx * 16`)
- generation status, block-entity count, error string if any

## Color modes (toggle in sidebar)

| # | mode | encoding | purpose |
|---|---|---|---|
| 1 | **Status** *(default)* | green=visited, yellow=generated-but-unvisited, dark gray=empty stub, gray=partial-gen-only, red=error | Categorical big-picture purge view. |
| 2 | **Inhabited time (log)** | white→deep blue on `max_inhabited_ticks`, log scale | Hot zones (bases) jump out; flyover stays light. |
| 3 | **Block entities (log)** | white→orange on summed BE count | Catches chunkloader-driven automation even with low inhabited time. |
| 4 | Last modified | white→purple on max `last_modified` per region | "Is this still in use?" |
| 5 | Chunk completeness | red→green on fraction of chunks with `status='minecraft:full'` | Region-of-cruft detection. |

Toggling is instant — same data, repaint only.

## Interaction model

- **Hover:** tooltip with quick stats.
- **Click region:** drill in. **Double-click region:** drill in directly.
- **Click chunk:** open inspector. **Esc:** back to world map.
- **Sidebar filters:** dimension toggle (Overworld/Nether/End), min-inhabited
  threshold slider (keeps regions whose busiest chunk clears the threshold),
  "hide empty regions" toggle.
- **Running counters:** regions visible, total file size, matches-filter count.

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

Three endpoints, JSON responses, SQLite opened read-only.

### `GET /api/world?dim=overworld`

Per-region rollup for one dimension. One object per region file, including
empty/error ones. Generated once per dimension and cached in memory (the data is
static during a session).

```json
[
  { "rx": -2, "rz": -5, "scan_status": "ok",
    "chunks_present": 1024, "chunks_visited": 1024,
    "max_inh": 348909120, "sum_inh": 2434125789,
    "sum_be": 7314, "chunks_full": 1024,
    "max_modified": 1779685074, "file_size": 12345678 }
]
```

### `GET /api/region/<dim>/<rx>/<rz>`

The region's scan-state row plus all of its chunks.

```json
{ "region": { "...": "..." },
  "chunks": [
    { "cx": -64, "cz": -160, "inhabited_ticks": 6088963, "last_modified": 1779684270,
      "status": "minecraft:full", "block_entities_count": 34, "error": null }
  ] }
```

### `GET /api/chunk/<dim>/<cx>/<cz>`

One chunk. Convenience endpoint; same data as a filtered region call.

> Region/chunk coordinates can be negative, so the routes use a custom signed-int
> URL converter (`<sint:...>`) — Flask's built-in `<int:>` rejects negatives.

## File layout

```
viz/
├── app.py              # Flask app, all backend logic
└── static/
    ├── index.html      # the single page
    ├── app.js          # all frontend logic (no build step)
    └── style.css
```

See the top-level `README.md` for how to run it.

## Out of scope

- Purge actions / "mark for delete" UX.
- Block-level rendering (that's what map renderers like Overviewer are for).
- Authentication, multi-user.
- Persistent annotations ("this region is the iron farm").
- Writing to the database, or rescanning from the web UI.
