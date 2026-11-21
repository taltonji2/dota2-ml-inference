"""Momentum curves and turning points from per-minute rows.

Momentum is a formula, not a model, so it stays explainable:

    gold_share(t) = gold_player / (gold_player + gold_opponent) - 0.5
    xp_share(t)   = xp_player   / (xp_player   + xp_opponent)   - 0.5
    momentum(t)   = gold_share + xp_share            # range -1 .. 1

A *swing* is a change in momentum of at least ``swing_threshold`` within
``window`` minutes. Events that fall inside the window are attached as likely causes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Swing:
    start_minute: int
    end_minute: int
    change: float
    direction: str
    events: list[dict[str, Any]] = field(default_factory=list)


def _share(a: float | None, b: float | None) -> float:
    if a is None or b is None or (a + b) <= 0:
        return 0.0
    return a / (a + b) - 0.5


def momentum_series(player: list[dict], opponent: list[dict]) -> list[dict[str, Any]]:
    """Both inputs are per-minute rows with gold and xp, ordered by minute."""
    opp = {r["minute"]: r for r in opponent}
    out = []
    for r in player:
        o = opp.get(r["minute"])
        if o is None:
            continue
        g = _share(r["gold"], o["gold"])
        x = _share(r["xp"], o["xp"])
        out.append(
            {
                "minute": r["minute"],
                "gold_diff": (r["gold"] or 0) - (o["gold"] or 0),
                "xp_diff": (r["xp"] or 0) - (o["xp"] or 0),
                "momentum": round(g + x, 4),
            }
        )
    return out


def detect_swings(
    series: list[dict], *, window: int = 3, swing_threshold: float = 0.15, min_minute: int = 2
) -> list[Swing]:
    """Find runs where momentum moved by at least the threshold within ``window`` minutes.

    Each swing starts at the last peak (or trough) before the move, and back-to-back
    swings in the same direction are merged into one. Minutes before ``min_minute``
    are skipped, because shares jump from zero at the start of every game.
    """
    series = [s for s in series if s["minute"] >= min_minute]
    vals = [s["momentum"] for s in series]
    found: list[tuple[int, int]] = []
    i = 0
    while i < len(vals) - 1:
        best_j, best_delta = None, 0.0
        for j in range(i + 1, min(i + window, len(vals) - 1) + 1):
            d = vals[j] - vals[i]
            if abs(d) >= swing_threshold and abs(d) > abs(best_delta):
                best_j, best_delta = j, d
        if best_j is None:
            i += 1
            continue
        # start from the latest extreme before the move, so flat stretches are trimmed
        pick = max if best_delta < 0 else min
        extreme = pick(vals[i:best_j])
        start = max(k for k in range(i, best_j) if vals[k] == extreme)
        if found and found[-1][1] == start and (vals[found[-1][1]] - vals[found[-1][0]]) * best_delta > 0:
            found[-1] = (found[-1][0], best_j)
        else:
            found.append((start, best_j))
        i = best_j
    swings = []
    for a, b in found:
        delta = vals[b] - vals[a]
        swings.append(
            Swing(
                start_minute=series[a]["minute"],
                end_minute=series[b]["minute"],
                change=round(delta, 4),
                direction="up" if delta > 0 else "down",
            )
        )
    return swings


# ---------------------------------------------------------------------------
# Database-backed helpers used by the API
# ---------------------------------------------------------------------------
def _minutes(conn: sqlite3.Connection, match_id: int, slot: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT minute, gold, xp, last_hits, denies FROM player_minutes "
            "WHERE match_id=? AND player_slot=? ORDER BY minute",
            (match_id, slot),
        )
    ]


def lane_opponent(conn: sqlite3.Connection, match_id: int, slot: int) -> int | None:
    """Enemy in the same physical lane with the most last hits at 10 minutes.

    Falls back to the enemy with the mirrored lane role (safe vs off, mid vs mid).
    """
    me = conn.execute(
        "SELECT is_radiant, lane, lane_role FROM match_players WHERE match_id=? AND player_slot=?",
        (match_id, slot),
    ).fetchone()
    if me is None:
        return None
    mirror = {1: 3, 2: 2, 3: 1}.get(me["lane_role"])
    rows = conn.execute(
        """SELECT mp.player_slot, mp.lane, mp.lane_role, COALESCE(pm.last_hits, 0) AS lh10
           FROM match_players mp
           LEFT JOIN player_minutes pm
             ON pm.match_id = mp.match_id AND pm.player_slot = mp.player_slot AND pm.minute = 10
           WHERE mp.match_id=? AND mp.is_radiant != ?
           ORDER BY lh10 DESC""",
        (match_id, me["is_radiant"]),
    ).fetchall()
    for r in rows:
        if me["lane"] is not None and r["lane"] == me["lane"]:
            return r["player_slot"]
    for r in rows:
        if mirror is not None and r["lane_role"] == mirror:
            return r["player_slot"]
    return rows[0]["player_slot"] if rows else None


def player_momentum(conn: sqlite3.Connection, match_id: int, slot: int, **swing_kw) -> dict[str, Any] | None:
    opp = lane_opponent(conn, match_id, slot)
    mine = _minutes(conn, match_id, slot)
    if opp is None or not mine:
        return None
    series = momentum_series(mine, _minutes(conn, match_id, opp))
    swings = detect_swings(series, **swing_kw)
    for s in swings:
        s.events = [
            dict(r)
            for r in conn.execute(
                "SELECT t_s, kind, detail, player_slot FROM events "
                "WHERE match_id=? AND player_slot IN (?, ?) AND kind IN ('kill','death','purchase') "
                "AND t_s BETWEEN ? AND ? ORDER BY t_s",
                (match_id, slot, opp, s.start_minute * 60, s.end_minute * 60),
            )
        ]
    return {
        "match_id": match_id,
        "player_slot": slot,
        "opponent_slot": opp,
        "series": series,
        "swings": [asdict(s) for s in swings],
    }


def team_advantage(conn: sqlite3.Connection, match_id: int) -> list[dict[str, Any]]:
    """Radiant minus Dire cumulative gold and XP per minute."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT pm.minute,
                      SUM(CASE WHEN mp.is_radiant THEN pm.gold ELSE -pm.gold END) AS gold_adv,
                      SUM(CASE WHEN mp.is_radiant THEN pm.xp ELSE -pm.xp END) AS xp_adv
               FROM player_minutes pm
               JOIN match_players mp USING (match_id, player_slot)
               WHERE pm.match_id=?
               GROUP BY pm.minute ORDER BY pm.minute""",
            (match_id,),
        )
    ]


def turning_points(conn: sqlite3.Connection, match_id: int, *, window: int = 3, top_n: int = 3) -> dict[str, Any]:
    """Lead changes plus the largest swings in team gold advantage."""
    adv = team_advantage(conn, match_id)
    points: list[dict[str, Any]] = []
    for prev, cur in zip(adv, adv[1:]):
        if prev["gold_adv"] and cur["gold_adv"] and (prev["gold_adv"] > 0) != (cur["gold_adv"] > 0):
            points.append(
                {
                    "minute": cur["minute"],
                    "kind": "lead_change",
                    "leader": "radiant" if cur["gold_adv"] > 0 else "dire",
                    "gold_adv": cur["gold_adv"],
                }
            )
    swings = []
    for i in range(len(adv) - window):
        delta = adv[i + window]["gold_adv"] - adv[i]["gold_adv"]
        swings.append((abs(delta), i, delta))
    used: set[int] = set()
    for _, i, delta in sorted(swings, reverse=True):
        if len([p for p in points if p["kind"] == "swing"]) >= top_n:
            break
        if any(abs(i - u) < window for u in used):
            continue
        used.add(i)
        points.append(
            {
                "minute": adv[i]["minute"],
                "end_minute": adv[i + window]["minute"],
                "kind": "swing",
                "toward": "radiant" if delta > 0 else "dire",
                "gold_change": delta,
            }
        )
    for p in points:
        start = p["minute"] * 60 - (60 if p["kind"] == "lead_change" else 0)
        end = p.get("end_minute", p["minute"]) * 60
        p["events"] = [
            dict(r)
            for r in conn.execute(
                "SELECT t_s, kind, detail, player_slot FROM events WHERE match_id=? "
                "AND kind IN ('kill','objective') AND t_s BETWEEN ? AND ? ORDER BY t_s",
                (match_id, start, end),
            )
        ]
    points.sort(key=lambda p: p["minute"])
    return {"match_id": match_id, "advantage": adv, "turning_points": points}
