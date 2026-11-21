"""Pull matches from OpenDota and store compact rows in SQLite.

Usage:
    python -m dota.ingest sample --bracket ancient_divine --limit 50 --request-parses
    python -m dota.ingest pending          # re-fetch matches whose parse was requested
    python -m dota.ingest match 8012345678 # one match by id

Raw JSON is never kept: each match is turned into rows and dropped, so memory
stays flat however many matches are ingested.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any, Iterable

from dota import db
from dota.brackets import BRACKETS, bracket_for
from dota.opendota import DailyBudgetExceeded, OpenDotaClient, is_parsed

log = logging.getLogger("dota.ingest")

HERO_CACHE = Path("data/heroes.json")


# ---------------------------------------------------------------------------
# Pure transforms (unit tested without the network)
# ---------------------------------------------------------------------------
def match_row(m: dict[str, Any], avg_rank_tier: float | None = None) -> dict[str, Any]:
    if avg_rank_tier is None:
        ranks = [p["rank_tier"] for p in m.get("players", []) if p.get("rank_tier")]
        avg_rank_tier = statistics.fmean(ranks) if ranks else None
    return {
        "match_id": m["match_id"],
        "start_time": m.get("start_time", 0),
        "duration_s": m.get("duration", 0),
        "patch": m.get("patch"),
        "avg_rank_tier": avg_rank_tier,
        "bracket": bracket_for(avg_rank_tier),
        "radiant_win": int(bool(m.get("radiant_win"))),
        "parsed_at": int(time.time()) if is_parsed(m) else None,
    }


def player_rows(m: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for p in m.get("players", []):
        slot = p["player_slot"]
        rows.append(
            {
                "match_id": m["match_id"],
                "player_slot": slot,
                "account_id": p.get("account_id"),
                "hero_id": p["hero_id"],
                "is_radiant": int(p.get("isRadiant", slot < 128)),
                "lane": p.get("lane"),
                "lane_role": p.get("lane_role"),
                "rank_tier": p.get("rank_tier"),
            }
        )
    return rows


def minute_rows(m: dict[str, Any]) -> list[tuple]:
    rows = []
    for p in m.get("players", []):
        gold, xp = p.get("gold_t") or [], p.get("xp_t") or []
        lh, dn = p.get("lh_t") or [], p.get("dn_t") or []
        for minute in range(len(gold)):
            rows.append(
                (
                    m["match_id"],
                    p["player_slot"],
                    minute,
                    gold[minute],
                    xp[minute] if minute < len(xp) else None,
                    lh[minute] if minute < len(lh) else None,
                    dn[minute] if minute < len(dn) else None,
                )
            )
    return rows


def event_rows(m: dict[str, Any], hero_names: dict[str, int] | None = None) -> list[tuple]:
    """Kills, deaths, purchases, wards and objectives as (match, slot, t, kind, detail)."""
    mid = m["match_id"]
    hero_to_slot = {p["hero_id"]: p["player_slot"] for p in m.get("players", [])}
    rows: list[tuple] = []
    for p in m.get("players", []):
        slot = p["player_slot"]
        for k in p.get("kills_log") or []:
            rows.append((mid, slot, k["time"], "kill", k.get("key")))
            victim_id = (hero_names or {}).get(k.get("key"))
            if victim_id in hero_to_slot:
                rows.append((mid, hero_to_slot[victim_id], k["time"], "death", None))
        for item in p.get("purchase_log") or []:
            rows.append((mid, slot, item["time"], "purchase", item.get("key")))
        for w in p.get("obs_log") or []:
            rows.append((mid, slot, w["time"], "obs_ward", json.dumps([w.get("x"), w.get("y")])))
        for w in p.get("sen_log") or []:
            rows.append((mid, slot, w["time"], "sen_ward", json.dumps([w.get("x"), w.get("y")])))
    for o in m.get("objectives") or []:
        rows.append((mid, None, o.get("time", 0), "objective", o.get("type")))
    return rows


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def store_match(
    conn: sqlite3.Connection,
    m: dict[str, Any],
    *,
    avg_rank_tier: float | None = None,
    hero_names: dict[str, int] | None = None,
) -> bool:
    """Upsert one match. Returns True when time series were stored."""
    mid = m["match_id"]
    row = match_row(m, avg_rank_tier)
    with conn:
        conn.execute(
            """INSERT INTO matches (match_id, start_time, duration_s, patch, avg_rank_tier, bracket, radiant_win, parsed_at)
               VALUES (:match_id, :start_time, :duration_s, :patch, :avg_rank_tier, :bracket, :radiant_win, :parsed_at)
               ON CONFLICT(match_id) DO UPDATE SET
                 duration_s=excluded.duration_s, patch=excluded.patch,
                 avg_rank_tier=COALESCE(matches.avg_rank_tier, excluded.avg_rank_tier),
                 bracket=COALESCE(matches.bracket, excluded.bracket),
                 radiant_win=excluded.radiant_win, parsed_at=excluded.parsed_at""",
            row,
        )
        conn.executemany(
            """INSERT OR REPLACE INTO match_players
               (match_id, player_slot, account_id, hero_id, is_radiant, lane, lane_role, rank_tier)
               VALUES (:match_id, :player_slot, :account_id, :hero_id, :is_radiant, :lane, :lane_role, :rank_tier)""",
            player_rows(m),
        )
        if row["parsed_at"]:
            conn.execute("DELETE FROM player_minutes WHERE match_id=?", (mid,))
            conn.execute("DELETE FROM events WHERE match_id=?", (mid,))
            conn.executemany("INSERT INTO player_minutes VALUES (?,?,?,?,?,?,?)", minute_rows(m))
            conn.executemany(
                "INSERT INTO events (match_id, player_slot, t_s, kind, detail) VALUES (?,?,?,?,?)",
                event_rows(m, hero_names),
            )
    return bool(row["parsed_at"])


def load_hero_names(client: OpenDotaClient) -> dict[str, int]:
    if HERO_CACHE.exists():
        return json.loads(HERO_CACHE.read_text())
    names = client.hero_names()
    HERO_CACHE.parent.mkdir(parents=True, exist_ok=True)
    HERO_CACHE.write_text(json.dumps(names))
    return names


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_sample(conn, client, bracket: str, limit: int, request_parses: bool) -> None:
    lo, hi = BRACKETS[bracket]
    heroes = load_hero_names(client)
    seen = {r[0] for r in conn.execute("SELECT match_id FROM matches")}
    stored = parsed = requested = 0
    cursor = None
    while stored < limit:
        page = client.public_matches(min_rank=lo, max_rank=hi, less_than_match_id=cursor)
        if not page:
            break
        cursor = min(p["match_id"] for p in page)
        for pm in page:
            if stored >= limit:
                break
            if pm["match_id"] in seen:
                continue
            m = client.match(pm["match_id"])
            got_series = store_match(conn, m, avg_rank_tier=pm.get("avg_rank_tier"), hero_names=heroes)
            seen.add(pm["match_id"])
            stored += 1
            parsed += got_series
            if not got_series and request_parses:
                client.request_parse(pm["match_id"])
                with conn:
                    conn.execute(
                        "UPDATE matches SET parse_requested_at=? WHERE match_id=?",
                        (int(time.time()), pm["match_id"]),
                    )
                requested += 1
    log.info("%s: stored %d matches, %d parsed, %d parse requests", bracket, stored, parsed, requested)


def cmd_pending(conn, client, min_age_s: int = 600) -> None:
    heroes = load_hero_names(client)
    cutoff = int(time.time()) - min_age_s
    ids: Iterable[int] = [
        r[0]
        for r in conn.execute(
            "SELECT match_id FROM matches WHERE parsed_at IS NULL AND parse_requested_at <= ?", (cutoff,)
        )
    ]
    done = 0
    for mid in ids:
        done += store_match(conn, client.match(mid), hero_names=heroes)
    log.info("pending: %d now parsed", done)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m dota.ingest")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--daily-budget", type=int, default=1800)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample", help="sample recent public matches in one bracket")
    s.add_argument("--bracket", choices=sorted(BRACKETS), required=True)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--request-parses", action="store_true")
    sub.add_parser("pending", help="re-fetch matches whose parse was requested")
    one = sub.add_parser("match", help="ingest one match by id")
    one.add_argument("match_id", type=int)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = db.connect(args.db)
    db.init_db(conn)
    client = OpenDotaClient(daily_budget=args.daily_budget)
    try:
        if args.cmd == "sample":
            cmd_sample(conn, client, args.bracket, args.limit, args.request_parses)
        elif args.cmd == "pending":
            cmd_pending(conn, client)
        else:
            store_match(conn, client.match(args.match_id), hero_names=load_hero_names(client))
    except DailyBudgetExceeded as e:
        log.warning("%s; stopping, rerun tomorrow to resume", e)
    finally:
        log.info("calls used this run: %d, headers: %s", client.rate.calls_made, client.rate.last_headers)
        client.close()
        conn.close()


if __name__ == "__main__":
    main()
