"""Day-one data spike: can we get enough parsed public matches per bracket?

For each bracket it pulls one page of recent public matches, fetches up to
--per-bracket of them, and records:
  * how many are already parsed (have per-minute time series)
  * which expected fields are present
  * the rate-limit headers OpenDota sends back
Optionally it requests parses for unparsed matches, so a second run a few
minutes later shows how many parse requests succeed.

Writes reports/spike-<timestamp>.json and prints a summary table.
Budget: about 4 x (1 + per_bracket) calls, plus 10 per parse request.

    python scripts/spike.py --per-bracket 20
    python scripts/spike.py --per-bracket 5 --request-parses
    python scripts/spike.py --recheck reports/spike-XXXX.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dota.brackets import BRACKETS  # noqa: E402
from dota.opendota import OpenDotaClient, is_parsed  # noqa: E402

PLAYER_FIELDS = [
    "gold_t", "xp_t", "lh_t", "dn_t", "times", "lane", "lane_role", "lane_pos",
    "kills_log", "purchase_log", "obs_log", "sen_log", "camps_stacked", "rank_tier", "account_id",
]
MATCH_FIELDS = ["radiant_gold_adv", "radiant_xp_adv", "objectives", "teamfights", "patch", "version"]
PUBLIC_FIELDS = ["avg_rank_tier", "num_rank_tier"]


def field_presence(objs: list[dict], fields: list[str]) -> dict[str, float]:
    if not objs:
        return {f: 0.0 for f in fields}
    return {f: round(sum(o.get(f) is not None for o in objs) / len(objs), 2) for f in fields}


def run(per_bracket: int, request_parses: bool) -> dict:
    client = OpenDotaClient()
    report: dict = {"started": int(time.time()), "brackets": {}}
    try:
        for name, (lo, hi) in BRACKETS.items():
            page = client.public_matches(min_rank=lo, max_rank=hi)
            sample = page[:per_bracket]
            matches, parsed, requested = [], [], []
            for pm in sample:
                m = client.match(pm["match_id"])
                matches.append(m)
                if is_parsed(m):
                    parsed.append(pm["match_id"])
                elif request_parses:
                    client.request_parse(pm["match_id"])
                    requested.append(pm["match_id"])
            players = [p for m in matches for p in m.get("players", [])]
            parsed_players = [p for m in matches if is_parsed(m) for p in m["players"]]
            report["brackets"][name] = {
                "rank_range": [lo, hi],
                "public_page_size": len(page),
                "avg_rank_tiers_seen": sorted({pm.get("avg_rank_tier") for pm in page if pm.get("avg_rank_tier")}),
                "public_fields": field_presence(page, PUBLIC_FIELDS),
                "fetched": len(matches),
                "parsed": len(parsed),
                "parsed_ids": parsed,
                "parse_requested_ids": requested,
                "player_fields_all": field_presence(players, PLAYER_FIELDS),
                "player_fields_parsed": field_presence(parsed_players, PLAYER_FIELDS),
                "match_fields": field_presence(matches, MATCH_FIELDS),
                "public_profile_share": field_presence(players, ["account_id"])["account_id"],
            }
            print(f"{name:16s} page={len(page):3d} fetched={len(matches):3d} parsed={len(parsed):3d} "
                  f"requested={len(requested):3d}", flush=True)
    finally:
        report["calls_used"] = client.rate.calls_made
        report["rate_limit_headers"] = client.rate.last_headers
        client.close()
    return report


def recheck(path: Path) -> dict:
    """Fetch previously requested matches again and count how many are now parsed."""
    old = json.loads(path.read_text())
    client = OpenDotaClient()
    out = {}
    try:
        for name, b in old["brackets"].items():
            ids = b.get("parse_requested_ids", [])
            now = sum(is_parsed(client.match(i)) for i in ids)
            out[name] = {"requested": len(ids), "now_parsed": now}
            print(f"{name:16s} requested={len(ids):3d} now_parsed={now:3d}")
    finally:
        client.close()
    return {"recheck_of": str(path), "results": out, "rate_limit_headers": client.rate.last_headers}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-bracket", type=int, default=20)
    ap.add_argument("--request-parses", action="store_true")
    ap.add_argument("--recheck", type=Path)
    args = ap.parse_args()

    report = recheck(args.recheck) if args.recheck else run(args.per_bracket, args.request_parses)
    out_dir = Path("reports")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"spike-{int(time.time())}.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport written to {out}")
    print(f"rate-limit headers: {report.get('rate_limit_headers')}")


if __name__ == "__main__":
    main()
