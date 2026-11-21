"""SQLite schema and connection helpers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = os.environ.get("DOTA_DB", "data/dota.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
  match_id      INTEGER PRIMARY KEY,
  start_time    INTEGER NOT NULL,
  duration_s    INTEGER NOT NULL,
  patch         INTEGER,
  avg_rank_tier REAL,
  bracket       TEXT,
  radiant_win   INTEGER NOT NULL,
  parsed_at     INTEGER,           -- NULL until time series is stored
  parse_requested_at INTEGER
);

CREATE TABLE IF NOT EXISTS match_players (
  match_id     INTEGER NOT NULL REFERENCES matches(match_id),
  player_slot  INTEGER NOT NULL,
  account_id   INTEGER,            -- NULL when the profile is private
  hero_id      INTEGER NOT NULL,
  is_radiant   INTEGER NOT NULL,
  lane         INTEGER,            -- physical lane: 1 bot, 2 mid, 3 top, 4/5 jungle/roam
  lane_role    INTEGER,            -- 1 safe, 2 mid, 3 off, 4 jungle
  rank_tier    INTEGER,
  PRIMARY KEY (match_id, player_slot)
);

CREATE TABLE IF NOT EXISTS player_minutes (
  match_id    INTEGER NOT NULL,
  player_slot INTEGER NOT NULL,
  minute      INTEGER NOT NULL,
  gold        INTEGER,             -- cumulative gold (OpenDota gold_t)
  xp          INTEGER,
  last_hits   INTEGER,
  denies      INTEGER,
  PRIMARY KEY (match_id, player_slot, minute)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
  match_id    INTEGER NOT NULL,
  player_slot INTEGER,             -- NULL for team/objective events
  t_s         INTEGER NOT NULL,    -- game time in seconds
  kind        TEXT NOT NULL,       -- kill, death, purchase, obs_ward, sen_ward, objective
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS events_match ON events(match_id, kind);

CREATE TABLE IF NOT EXISTS player_features (
  match_id    INTEGER NOT NULL,
  player_slot INTEGER NOT NULL,
  feature_set TEXT NOT NULL,
  features    TEXT NOT NULL,       -- JSON object of named numbers
  PRIMARY KEY (match_id, player_slot, feature_set)
);
"""


def connect(path: str | os.PathLike = DEFAULT_DB, *, read_only: bool = False) -> sqlite3.Connection:
    # FastAPI may open and close a request's connection on different worker threads,
    # so same-thread checks are off. Each connection still serves one request only.
    path = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
