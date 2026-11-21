"""Synthetic OpenDota-shaped match for tests and the offline demo.

Run ``python -m dota.demo`` to seed data/dota.sqlite so the dashboard works
without calling OpenDota.
"""

from __future__ import annotations

from dota import db
from dota.ingest import store_match

HERO_NAMES = {f"npc_dota_hero_h{i}": i for i in range(1, 11)}


def make_match(match_id: int = 1001, minutes: int = 20, parsed: bool = True) -> dict:
    """Synthetic OpenDota-shaped parsed match.

    Radiant safelaner (slot 0, lane 1) dominates Dire offlaner (slot 128, lane 1)
    early, then loses ground sharply from minute 12, and Dire takes the lead.
    """
    players = []
    for i in range(10):
        radiant = i < 5
        slot = i if radiant else 128 + (i - 5)
        lane, role = [(1, 1), (2, 2), (3, 3), (3, 3), (1, 1)][i % 5]
        if not radiant:
            # mirror lanes: Dire offlaner sits in bot lane (lane 1) against the Radiant safelaner
            lane, role = [(1, 3), (2, 2), (3, 1), (1, 3), (3, 1)][i - 5]
        gold, xp = [], []
        for t in range(minutes + 1):
            base = 400 * t
            if slot == 0:
                g = base * (1.5 if t < 12 else 1.5 - 0.12 * (t - 11))
            elif slot == 128:
                g = base * (0.8 if t < 12 else 0.8 + 0.12 * (t - 11))
            elif not radiant:
                g = base * (1.0 if t < 12 else 1.4)
            else:
                g = base
            gold.append(int(g))
            xp.append(int(g * 1.1))
        p = {
            "player_slot": slot,
            "isRadiant": radiant,
            "hero_id": i + 1,
            "account_id": 5000 + i if i % 2 == 0 else None,
            "lane": lane,
            "lane_role": role,
            "rank_tier": 64 if i < 8 else None,
        }
        if parsed:
            p.update(
                gold_t=gold,
                xp_t=xp,
                lh_t=[3 * t for t in range(minutes + 1)],
                dn_t=[t for t in range(minutes + 1)],
                kills_log=[],
                purchase_log=[{"time": 600, "key": "item_power_treads"}] if slot == 0 else [],
                obs_log=[{"time": 30, "x": 100, "y": 120}] if slot == 4 else [],
                sen_log=[],
            )
        players.append(p)
    if parsed:
        # Dire offlaner kills the Radiant safelaner twice during the collapse
        dire_off = next(p for p in players if p["player_slot"] == 128)
        dire_off["kills_log"] = [{"time": 760, "key": "npc_dota_hero_h1"}, {"time": 820, "key": "npc_dota_hero_h1"}]
    return {
        "match_id": match_id,
        "start_time": 1_763_000_000,
        "duration": minutes * 60,
        "patch": 58,
        "radiant_win": False,
        "players": players,
        "objectives": [{"time": 840, "type": "building_kill", "key": "npc_dota_goodguys_tower1_bot"}] if parsed else [],
    }


def seed(path: str = db.DEFAULT_DB) -> None:
    conn = db.connect(path)
    db.init_db(conn)
    store_match(conn, make_match(), avg_rank_tier=64, hero_names=HERO_NAMES)
    conn.close()
    print(f"seeded synthetic match 1001 into {path}")


if __name__ == "__main__":
    seed()
