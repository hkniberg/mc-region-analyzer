#!/usr/bin/env python3
"""Minecraft region browser — Flask backend.

Three endpoints over a chunks DB produced by scan_chunks.py:
  GET /api/world?dim=overworld          -> per-region rollup for one dimension
  GET /api/region/<dim>/<rx>/<rz>       -> all chunks in a region
  GET /api/chunk/<dim>/<cx>/<cz>        -> one chunk

DB opened read-only. Per-dim world rollups are cached in process memory.

The database path defaults to the bundled sample (../data/chunks_sample.db) so
the app runs out of the box. Point it at a full scan with the CHUNKS_DB env var:
  CHUNKS_DB=/path/to/chunks_full.db python3 app.py
"""
import os
import sqlite3
from flask import Flask, jsonify, send_from_directory, abort, request
from werkzeug.routing import BaseConverter

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "chunks_sample.db")
DB_PATH = os.path.abspath(os.environ.get("CHUNKS_DB") or _DEFAULT_DB)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VALID_DIMS = ("overworld", "nether", "end")

app = Flask(__name__, static_folder=None)


class SignedIntConverter(BaseConverter):
    """Like Flask's int converter but accepts negative integers."""
    regex = r"-?\d+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(value)


app.url_map.converters["sint"] = SignedIntConverter

_world_cache = {}


def open_db():
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def validate_dim(dim):
    if dim not in VALID_DIMS:
        abort(400, description=f"dim must be one of {VALID_DIMS}")


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/api/world")
def api_world():
    dim = request.args.get("dim", "overworld")
    validate_dim(dim)
    if dim in _world_cache:
        return jsonify(_world_cache[dim])

    db = open_db()
    # Per-region rollup. LEFT JOIN so empty/error regions appear too.
    rows = db.execute(
        """
        SELECT r.rx, r.rz, r.scan_status, r.file_size, r.error,
               c.chunks_present, c.chunks_visited,
               c.max_inh, c.sum_inh, c.sum_be,
               c.chunks_full, c.max_modified
        FROM regions r
        LEFT JOIN (
            SELECT dim, rx, rz,
                   COUNT(*)                                                    AS chunks_present,
                   SUM(CASE WHEN inhabited_ticks > 0 THEN 1 ELSE 0 END)        AS chunks_visited,
                   MAX(inhabited_ticks)                                        AS max_inh,
                   SUM(inhabited_ticks)                                        AS sum_inh,
                   SUM(block_entities_count)                                   AS sum_be,
                   SUM(CASE WHEN status = 'minecraft:full' THEN 1 ELSE 0 END)  AS chunks_full,
                   MAX(last_modified)                                          AS max_modified
            FROM chunks
            GROUP BY dim, rx, rz
        ) c ON c.dim = r.dim AND c.rx = r.rx AND c.rz = r.rz
        WHERE r.dim = ?
        """,
        (dim,),
    ).fetchall()
    db.close()

    out = [dict(r) for r in rows]
    _world_cache[dim] = out
    return jsonify(out)


@app.route("/api/region/<dim>/<sint:rx>/<sint:rz>")
def api_region(dim, rx, rz):
    validate_dim(dim)
    db = open_db()
    region = db.execute(
        "SELECT dim, rx, rz, path, file_size, file_mtime, scanned_at, scan_status, error, chunks_present, chunks_with_errors FROM regions WHERE dim=? AND rx=? AND rz=?",
        (dim, rx, rz),
    ).fetchone()
    if region is None:
        db.close()
        abort(404, description=f"no region row for {dim} ({rx},{rz})")
    chunks = db.execute(
        "SELECT cx, cz, inhabited_ticks, last_modified, status, block_entities_count, error FROM chunks WHERE dim=? AND rx=? AND rz=? ORDER BY cz, cx",
        (dim, rx, rz),
    ).fetchall()
    db.close()
    return jsonify({
        "region": dict(region),
        "chunks": [dict(c) for c in chunks],
    })


@app.route("/api/chunk/<dim>/<sint:cx>/<sint:cz>")
def api_chunk(dim, cx, cz):
    validate_dim(dim)
    db = open_db()
    row = db.execute(
        "SELECT dim, cx, cz, rx, rz, inhabited_ticks, last_modified, status, block_entities_count, error FROM chunks WHERE dim=? AND cx=? AND cz=?",
        (dim, cx, cz),
    ).fetchone()
    db.close()
    if row is None:
        abort(404, description=f"no chunk {dim} ({cx},{cz})")
    return jsonify(dict(row))


if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"chunks DB not found: {DB_PATH}\nSet CHUNKS_DB to a database produced by scan_chunks.py.")
    print(f" * Serving chunks DB: {DB_PATH}")
    app.run(host="0.0.0.0", port=8089, debug=False)
