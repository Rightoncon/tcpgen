"""SQLite persistence (spec §4). Thin wrapper over stdlib sqlite3 — no
ORM, parameterized queries only, same "single-file, no build step" spirit
as the rest of this project.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = Path(__file__).resolve().parent / "tcp.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS plan (
  id                INTEGER PRIMARY KEY,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'draft',
  job_type          TEXT,
  address           TEXT NOT NULL,
  city              TEXT NOT NULL,
  state             TEXT NOT NULL DEFAULT 'CA',
  zip               TEXT,
  jurisdiction      TEXT,
  permit_number     TEXT,
  job_number        TEXT,
  center_lat        REAL NOT NULL,
  center_lng        REAL NOT NULL,
  work_polygon      TEXT,
  work_description  TEXT,
  scope             TEXT NOT NULL,
  sidewalk_affected INTEGER NOT NULL DEFAULT 0,
  parking_affected  INTEGER NOT NULL DEFAULT 0,
  duration          TEXT NOT NULL DEFAULT 'short_term',
  posted_speed      INTEGER,
  ta_figure         TEXT,
  road_osm_id       INTEGER,
  road_name         TEXT,
  road_geometry     TEXT,
  road_width_ft     REAL,
  road_lanes        INTEGER,
  road_bearing_deg  REAL,
  parcel_apn        TEXT,
  parcel_polygon    TEXT,
  frontage_source   TEXT NOT NULL DEFAULT 'parcel',
  frontage_length_ft REAL,
  corner_lot        INTEGER NOT NULL DEFAULT 0,
  reference_image   TEXT,
  notes_override    TEXT,
  pdf_sheet1_path   TEXT,
  pdf_sheet2_path   TEXT
);

CREATE TABLE IF NOT EXISTS device (
  id          INTEGER PRIMARY KEY,
  plan_id     INTEGER NOT NULL REFERENCES plan(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  code        TEXT,
  label       TEXT,
  station_ft  REAL NOT NULL,
  offset_ft   REAL NOT NULL,
  lat         REAL NOT NULL,
  lng         REAL NOT NULL,
  approach    TEXT,
  seq         INTEGER NOT NULL,
  locked      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
  id        INTEGER PRIMARY KEY,
  plan_id   INTEGER NOT NULL,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_plan_id ON device(plan_id);
CREATE INDEX IF NOT EXISTS idx_audit_plan_id ON audit(plan_id);
"""


# Columns added after the initial schema -- ALTER TABLE ADD COLUMN, run
# once and ignored (via the duplicate-column error) on every later start.
# Simpler than a migration framework for a single-file SQLite app.
_MIGRATIONS = [
    "ALTER TABLE plan ADD COLUMN job_type TEXT",
    "ALTER TABLE plan ADD COLUMN pdf_notes_path TEXT",
    "ALTER TABLE plan ADD COLUMN usa_ticket TEXT",  # USA North 811 ticket # (2026-09-28)
]


def init_db(db_path: Path = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log_audit(conn: sqlite3.Connection, plan_id: int, actor: str, action: str, detail: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO audit (plan_id, at, actor, action, detail) VALUES (?, datetime('now'), ?, ?, ?)",
        (plan_id, actor, action, detail),
    )
