"""Download Dota 2 replays and parse them locally into tick-level SQLite rows.

OpenDota finds matches and supplies each replay's download URL (it appears once
OpenDota knows the replay salt, which a parse request triggers). This module:

1. streams the ``.dem.bz2`` from Valve's CDN and decompresses it to disk in
   fixed-size chunks (constant memory),
2. parses the ``.dem`` with gem-dota, streaming hero state and the combat log
   straight into SQLite in batches instead of holding them in memory,
3. derives per-minute rows and kill/death events into the main database, so
   the momentum endpoints work even for matches OpenDota never parsed,
4. deletes the replay file and records size, time and peak memory.

Usage:
    python -m dota.replays run --limit 5                    # matches already in the DB with a replay URL
    python -m dota.replays match 8012345678                 # one match by id (fetches the URL from OpenDota)
    python -m dota.replays file path/to/8012345678.dem      # a replay you already have, e.g. your own games
    python -m dota.replays run --limit 5 --sample-ticks 30  # one sample per second instead of every tick

Size guide, measured per match in ``replay_parses``: every tick (``--sample-ticks 1``)
is roughly 30 times the rows of one sample per second (``--sample-ticks 30``).
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import resource
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from dota import db

log = logging.getLogger("dota.replays")

TICKS_PER_SECOND = 30
DEFAULT_SAMPLE_TICKS = 1  # every tick: the "big data" setting
REPLAY_DIR = Path("data/replays_tmp")
CHUNK = 1 << 20  # 1 MiB


def slot_for_player_id(player_id: int) -> int:
    """Replay player ids run 0-9; OpenDota player_slot is 0-4 and 128-132."""
    return player_id if player_id < 5 else 128 + (player_id - 5)


class ReplayParseError(RuntimeError):
    pass


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux and bytes on macOS
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def download_replay(url: str, dest: Path, *, client: httpx.Client | None = None) -> Path:
    """Stream a .dem.bz2 (or plain .dem) to ``dest`` without loading it into memory."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    own = client is None
    client = client or httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        with client.stream("GET", url) as resp, open(tmp, "wb") as out:
            resp.raise_for_status()
            decomp = bz2.BZ2Decompressor() if url.endswith(".bz2") else None
            for chunk in resp.iter_bytes(CHUNK):
                out.write(decomp.decompress(chunk) if decomp else chunk)
        tmp.replace(dest)
        return dest
    finally:
        tmp.unlink(missing_ok=True)
        if own:
            client.close()


# ---------------------------------------------------------------------------
# Streaming sinks: gem-dota calls ``append``; rows go to SQLite in batches
# ---------------------------------------------------------------------------
class NameTable:
    """Interns unit/ability/item names into the ``names`` table."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.cache: dict[str, int] = {r[1]: r[0] for r in conn.execute("SELECT id, name FROM names")}

    def id(self, name: str | None) -> int | None:
        if not name:
            return None
        nid = self.cache.get(name)
        if nid is None:
            cur = self.conn.execute("INSERT OR IGNORE INTO names(name) VALUES (?)", (name,))
            nid = cur.lastrowid if cur.rowcount else self.conn.execute(
                "SELECT id FROM names WHERE name=?", (name,)
            ).fetchone()[0]
            self.cache[name] = nid
        return nid


class HeroTickSink:
    """List-like sink that replaces ``PlayerExtractor.snapshots``.

    gem-dota appends one snapshot per hero per sample; we write each batch to
    SQLite and keep only what the per-minute summary needs.
    """

    def __init__(self, conn: sqlite3.Connection, match_id: int, batch_size: int = 20_000) -> None:
        self.conn = conn
        self.match_id = match_id
        self.batch_size = batch_size
        self.buf: list[tuple] = []
        self.rows = 0
        self.hero_slot: dict[str, int] = {}  # npc_name -> player_slot
        # player_slot -> minute -> (earned_gold, earned_xp, last_hits, denies)
        self.minutes: dict[int, dict[int, tuple]] = {}

    def append(self, s: Any) -> None:
        slot = slot_for_player_id(s.player_id)
        self.hero_slot.setdefault(s.npc_name, slot)
        self.buf.append(
            (
                self.match_id, slot, s.tick, s.game_time_s,
                None if s.x is None else round(s.x), None if s.y is None else round(s.y),
                s.hp, s.max_hp, round(s.mana), round(s.max_mana), s.life_state, s.level,
                s.net_worth, s.gold, s.total_earned_gold, s.total_earned_xp, s.lh, s.dn,
                s.total_hero_damage, s.total_deaths,
            )
        )
        if s.game_time_s is not None and s.game_time_s >= 0:
            minute = s.game_time_s // 60
            per = self.minutes.setdefault(slot, {})
            if minute not in per:  # first sample at or after each minute boundary
                per[minute] = (s.total_earned_gold, s.total_earned_xp, s.lh, s.dn)
        if len(self.buf) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            self.conn.executemany(
                "INSERT OR REPLACE INTO hero_ticks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self.buf
            )
            self.rows += len(self.buf)
            self.buf.clear()

    # gem-dota only appends during the parse; these keep it list-compatible.
    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return self.rows + len(self.buf)


class CombatLogSink:
    """Writes every combat log entry that involves a hero."""

    def __init__(self, conn: sqlite3.Connection, match_id: int, names: NameTable, batch_size: int = 20_000) -> None:
        self.conn = conn
        self.match_id = match_id
        self.names = names
        self.batch_size = batch_size
        self.buf: list[tuple] = []
        self.rows = 0
        self.deaths: list[tuple[float, str, str]] = []  # (game_time_s, killer source, victim)

    def __call__(self, e: Any) -> None:
        if not (e.attacker_is_hero or e.target_is_hero):
            return
        kind = str(getattr(e.log_type, "name", e.log_type))
        if kind == "DEATH" and e.target_is_hero and not e.target_is_illusion and not e.will_reincarnate:
            self.deaths.append((e.game_time_s, e.damage_source_name or e.attacker_name, e.target_name))
        n = self.names.id
        self.buf.append(
            (
                self.match_id, e.tick, e.game_time_s, kind,
                n(e.attacker_name), n(e.damage_source_name), n(e.target_name),
                n(e.inflictor_name or e.value_name),
                int(e.value) if e.value is not None else None,
                int(bool(e.attacker_is_hero)), int(bool(e.target_is_hero)),
                None if e.location_x is None else round(e.location_x),
                None if e.location_y is None else round(e.location_y),
            )
        )
        if len(self.buf) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if self.buf:
            self.conn.executemany("INSERT INTO combat_log VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", self.buf)
            self.rows += len(self.buf)
            self.buf.clear()


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------
def _gem_parse(dem_path: Path, ticks: HeroTickSink, combat: CombatLogSink, sample_ticks: int) -> None:
    """Drive gem-dota's parser with our streaming sinks attached."""
    from gem.extractors.players import PlayerExtractor
    from gem.parser import ReplayParser

    parser = ReplayParser(dem_path)
    players = PlayerExtractor(sample_interval=sample_ticks, minute_snapshots=False)
    players.attach(parser)
    players.snapshots = ticks  # stream instead of accumulating in memory
    parser.on_combat_log_entry(combat)
    parser.parse()


def parse_replay(
    dem_path: Path,
    match_id: int,
    replay_conn: sqlite3.Connection,
    main_conn: sqlite3.Connection | None = None,
    *,
    sample_ticks: int = DEFAULT_SAMPLE_TICKS,
    parse_fn: Callable[[Path, HeroTickSink, CombatLogSink, int], None] = _gem_parse,
) -> dict[str, Any]:
    """Parse one replay into ``replay_conn``; optionally summarise into ``main_conn``."""
    started = time.monotonic()
    dem_mb = dem_path.stat().st_size / 1e6
    with replay_conn:
        replay_conn.execute("DELETE FROM hero_ticks WHERE match_id=?", (match_id,))
        replay_conn.execute("DELETE FROM combat_log WHERE match_id=?", (match_id,))
        names = NameTable(replay_conn)
        ticks = HeroTickSink(replay_conn, match_id)
        combat = CombatLogSink(replay_conn, match_id, names)
        parse_fn(dem_path, ticks, combat, sample_ticks)
        ticks.flush()
        combat.flush()
        if ticks.rows == 0:
            # gem-dota logs and stops on a corrupt or truncated file instead of raising;
            # raising here rolls back the transaction so nothing half-parsed is kept.
            raise ReplayParseError(f"{dem_path.name}: no hero data found; not a valid or complete replay")
        stats = {
            "match_id": match_id,
            "parsed_at": int(time.time()),
            "sample_ticks": sample_ticks,
            "dem_mb": round(dem_mb, 1),
            "parse_s": round(time.monotonic() - started, 1),
            "peak_rss_mb": round(_rss_mb(), 1),
            "tick_rows": ticks.rows,
            "combat_rows": combat.rows,
            "error": None,
        }
        replay_conn.execute(
            "INSERT OR REPLACE INTO replay_parses VALUES (:match_id, :parsed_at, :sample_ticks, :dem_mb, "
            ":parse_s, :peak_rss_mb, :tick_rows, :combat_rows, :error)",
            stats,
        )
    if main_conn is not None:
        summarise_into_main(main_conn, match_id, ticks, combat)
    return stats


def summarise_into_main(conn: sqlite3.Connection, match_id: int, ticks: HeroTickSink, combat: CombatLogSink) -> None:
    """Per-minute rows and kill/death events from the replay, for the existing endpoints."""
    minute_rows = [
        (match_id, slot, minute, g, xp, lh, dn)
        for slot, per in ticks.minutes.items()
        for minute, (g, xp, lh, dn) in sorted(per.items())
    ]
    events = []
    for t, killer, victim in combat.deaths:
        t_s = int(t or 0)
        if victim in ticks.hero_slot:
            events.append((match_id, ticks.hero_slot[victim], t_s, "death", killer))
        if killer in ticks.hero_slot:
            events.append((match_id, ticks.hero_slot[killer], t_s, "kill", victim))
    now = int(time.time())
    with conn:
        exists = conn.execute("SELECT parsed_at FROM matches WHERE match_id=?", (match_id,)).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO matches (match_id, start_time, duration_s, radiant_win, parsed_at, replay_parsed_at) "
                "VALUES (?, 0, ?, 0, ?, ?)",
                (match_id, 60 * max((m for per in ticks.minutes.values() for m in per), default=0), now, now),
            )
        # OpenDota's own per-minute rows win when present; the replay fills the gap otherwise.
        if exists is None or exists["parsed_at"] is None:
            conn.execute("DELETE FROM player_minutes WHERE match_id=?", (match_id,))
            conn.executemany("INSERT INTO player_minutes VALUES (?,?,?,?,?,?,?)", minute_rows)
            conn.execute("DELETE FROM events WHERE match_id=? AND kind IN ('kill','death')", (match_id,))
            conn.executemany(
                "INSERT INTO events (match_id, player_slot, t_s, kind, detail) VALUES (?,?,?,?,?)", events
            )
            conn.execute("UPDATE matches SET parsed_at=? WHERE match_id=?", (now, match_id))
        conn.execute("UPDATE matches SET replay_parsed_at=? WHERE match_id=?", (now, match_id))
        # Replays name the heroes directly; fill players OpenDota did not give us.
        hero_ids = _cached_hero_ids()
        for npc, slot in ticks.hero_slot.items():
            conn.execute(
                "INSERT OR IGNORE INTO match_players (match_id, player_slot, hero_id, is_radiant) VALUES (?,?,?,?)",
                (match_id, slot, hero_ids.get(npc, 0), int(slot < 128)),
            )


def _cached_hero_ids() -> dict[str, int]:
    """npc_dota_hero_* -> hero id, from the cache the ingest writes (empty if absent)."""
    from dota.ingest import HERO_CACHE

    try:
        return json.loads(HERO_CACHE.read_text())
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def process_match(
    match_id: int,
    url: str,
    main_conn: sqlite3.Connection,
    replay_conn: sqlite3.Connection,
    *,
    sample_ticks: int = DEFAULT_SAMPLE_TICKS,
    keep_replay: bool = False,
    download: Callable[[str, Path], Path] = download_replay,
    parse_fn=_gem_parse,
) -> dict[str, Any]:
    dest = REPLAY_DIR / f"{match_id}.dem"
    try:
        download(url, dest)
        stats = parse_replay(dest, match_id, replay_conn, main_conn, sample_ticks=sample_ticks, parse_fn=parse_fn)
        log.info(
            "match %s: %.0f MB replay, %s tick rows, %s combat rows, %.1fs, peak RSS %.0f MB",
            match_id, stats["dem_mb"], stats["tick_rows"], stats["combat_rows"], stats["parse_s"], stats["peak_rss_mb"],
        )
        return stats
    except Exception as exc:  # one bad replay must not stop a batch
        log.exception("match %s failed", match_id)
        with replay_conn:
            replay_conn.execute(
                "INSERT OR REPLACE INTO replay_parses (match_id, parsed_at, sample_ticks, error) VALUES (?,?,?,?)",
                (match_id, int(time.time()), sample_ticks, f"{type(exc).__name__}: {exc}"[:500]),
            )
        return {"match_id": match_id, "error": str(exc)}
    finally:
        if not keep_replay:
            dest.unlink(missing_ok=True)


def pending_replays(main_conn: sqlite3.Connection, replay_conn: sqlite3.Connection, limit: int) -> list[tuple[int, str]]:
    done = {r[0] for r in replay_conn.execute("SELECT match_id FROM replay_parses WHERE error IS NULL")}
    rows = main_conn.execute(
        "SELECT match_id, replay_url FROM matches WHERE replay_url IS NOT NULL ORDER BY start_time DESC"
    )
    return [(mid, url) for mid, url in rows if mid not in done][:limit]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m dota.replays")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--replay-db", default=db.DEFAULT_REPLAY_DB)
    ap.add_argument("--sample-ticks", type=int, default=DEFAULT_SAMPLE_TICKS,
                    help="ticks between hero samples: 1 = every tick (default), 30 = once a second")
    ap.add_argument("--keep-replay", action="store_true", help="keep the .dem file after parsing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="parse matches in the DB that have a replay URL")
    run.add_argument("--limit", type=int, default=5)
    one = sub.add_parser("match", help="fetch one match's replay URL from OpenDota and parse it")
    one.add_argument("match_id", type=int)
    f = sub.add_parser("file", help="parse a local .dem file")
    f.add_argument("path", type=Path)
    f.add_argument("--match-id", type=int, help="defaults to the number in the file name")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main_conn = db.connect(args.db)
    db.init_db(main_conn)
    replay_conn = db.connect(args.replay_db)
    db.init_replay_db(replay_conn)
    try:
        if args.cmd == "run":
            todo = pending_replays(main_conn, replay_conn, args.limit)
            log.info("%d replays to parse", len(todo))
            for mid, url in todo:
                process_match(mid, url, main_conn, replay_conn,
                              sample_ticks=args.sample_ticks, keep_replay=args.keep_replay)
        elif args.cmd == "match":
            from dota.ingest import load_hero_names, store_match
            from dota.opendota import OpenDotaClient

            client = OpenDotaClient()
            try:
                m = client.match(args.match_id)
                store_match(main_conn, m, hero_names=load_hero_names(client))
                if not m.get("replay_url"):
                    client.request_parse(args.match_id)
                    log.warning("no replay URL yet; parse requested. Retry in a few minutes.")
                    return
            finally:
                client.close()
            process_match(args.match_id, m["replay_url"], main_conn, replay_conn,
                          sample_ticks=args.sample_ticks, keep_replay=args.keep_replay)
        else:
            mid = args.match_id or int("".join(c for c in args.path.stem if c.isdigit()) or 0)
            if not mid:
                ap.error("pass --match-id; the file name has no match id")
            try:
                stats = parse_replay(args.path, mid, replay_conn, main_conn, sample_ticks=args.sample_ticks)
                log.info("parsed %s: %s", args.path, stats)
            except ReplayParseError as exc:
                log.error("%s", exc)
                raise SystemExit(1) from exc
    finally:
        main_conn.close()
        replay_conn.close()


if __name__ == "__main__":
    main()
