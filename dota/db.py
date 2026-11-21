"""SQLite schema and connection helpers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DB = os.environ.get("DOTA_DB", "data/dota.sqlite")
# Tick-level replay data lives in its own file: it is large, and the API and a
# future Lambda snapshot only need the small main database.
DEFAULT_REPLAY_DB = os.environ.get("DOTA_REPLAY_DB", "data/replays.sqlite")

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
  parse_requested_at INTEGER,
  replay_url    TEXT,              -- from OpenDota once the replay salt is known
  replay_parsed_at INTEGER         -- set when the full replay has been parsed locally
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


# Columns added after the first release; init_db adds them to older databases.
_ADDED_COLUMNS = {"matches": {"replay_url": "TEXT", "replay_parsed_at": "INTEGER"}}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, cols in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


REPLAY_SCHEMA = """
-- One row per hero per sampled tick (30 ticks = 1 second of game time).
CREATE TABLE IF NOT EXISTS hero_ticks (
  match_id     INTEGER NOT NULL,
  player_slot  INTEGER NOT NULL,
  tick         INTEGER NOT NULL,
  game_time_s  INTEGER,
  x            INTEGER,            -- world coordinates, rounded
  y            INTEGER,
  hp           INTEGER,
  max_hp       INTEGER,
  mana         INTEGER,
  max_mana     INTEGER,
  life_state   INTEGER,            -- 0 alive, 1 dying, 2 dead
  level        INTEGER,
  net_worth    INTEGER,
  gold         INTEGER,            -- unspent
  earned_gold  INTEGER,            -- cumulative
  earned_xp    INTEGER,            -- cumulative
  last_hits    INTEGER,
  denies       INTEGER,
  hero_damage  INTEGER,            -- cumulative hero damage dealt
  deaths       INTEGER,
  PRIMARY KEY (match_id, player_slot, tick)
) WITHOUT ROWID;

-- Interned unit, ability and item names keep the combat log compact.
CREATE TABLE IF NOT EXISTS names (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

-- Every combat log entry that involves a hero.
CREATE TABLE IF NOT EXISTS combat_log (
  match_id     INTEGER NOT NULL,
  tick         INTEGER NOT NULL,
  game_time_s  REAL,
  type         TEXT NOT NULL,      -- DAMAGE, HEAL, DEATH, ABILITY, ITEM, PURCHASE, GOLD, XP, ...
  attacker_id  INTEGER REFERENCES names(id),
  source_id    INTEGER REFERENCES names(id),
  target_id    INTEGER REFERENCES names(id),
  inflictor_id INTEGER REFERENCES names(id),
  value        INTEGER,
  attacker_hero INTEGER,
  target_hero  INTEGER,
  x            INTEGER,
  y            INTEGER
);
CREATE INDEX IF NOT EXISTS combat_log_match ON combat_log(match_id, type, tick);

-- One row per parse attempt, so size and memory use are measured, not guessed.
CREATE TABLE IF NOT EXISTS replay_parses (
  match_id      INTEGER PRIMARY KEY,
  parsed_at     INTEGER NOT NULL,
  sample_ticks  INTEGER NOT NULL,
  dem_mb        REAL,
  parse_s       REAL,
  peak_rss_mb   REAL,
  tick_rows     INTEGER,
  combat_rows   INTEGER,
  error         TEXT
);
"""


def init_replay_db(conn: sqlite3.Connection) -> None:
    conn.executescript(REPLAY_SCHEMA)
    conn.commit()
