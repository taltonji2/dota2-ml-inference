import bz2
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dota import db
from dota.replays import (
    CombatLogSink,
    HeroTickSink,
    NameTable,
    download_replay,
    parse_replay,
    pending_replays,
    process_match,
    slot_for_player_id,
)

HEROES = ["npc_dota_hero_axe", "npc_dota_hero_lina"]


def snap(pid, tick, **kw):
    base = dict(
        tick=tick, player_id=pid, npc_name=HEROES[0] if pid == 0 else HEROES[1], team=2 if pid < 5 else 3,
        level=1, xp=0, gold=600, net_worth=600, lh=tick // 300, dn=0, hp=600, max_hp=600, mana=300.4,
        max_mana=300.0, x=-6000.6, y=-5000.2, total_earned_gold=tick, total_earned_xp=tick * 2,
        game_time_s=(tick - 900) // 30, life_state=0, total_hero_damage=0, total_deaths=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def entry(kind, tick, attacker, target, attacker_hero=True, target_hero=True, **kw):
    base = dict(
        log_type=SimpleNamespace(name=kind), tick=tick, game_time_s=(tick - 900) / 30,
        attacker_name=attacker, damage_source_name=attacker, target_name=target, inflictor_name=None,
        value_name=None, value=100, attacker_is_hero=attacker_hero, target_is_hero=target_hero,
        target_is_illusion=False, will_reincarnate=False, location_x=1.4, location_y=2.6,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def fake_parse(dem_path, ticks, combat, sample_ticks):
    """Stands in for gem-dota: 3 minutes of game for two heroes, one kill."""
    for tick in range(900, 900 + 30 * 180 + 1, sample_ticks):
        ticks.append(snap(0, tick))
        ticks.append(snap(5, tick, x=6000.0, y=5000.0))
    combat(entry("DAMAGE", 2000, HEROES[0], HEROES[1]))
    combat(entry("DAMAGE", 2001, "npc_dota_creep_goodguys_melee", "npc_dota_creep_badguys_melee",
                 attacker_hero=False, target_hero=False))
    combat(entry("DEATH", 3000, HEROES[0], HEROES[1]))
    combat(entry("PURCHASE", 3100, HEROES[0], HEROES[0], value_name="item_blink"))


@pytest.fixture
def conns(tmp_path):
    main = db.connect(tmp_path / "main.sqlite")
    db.init_db(main)
    rep = db.connect(tmp_path / "replays.sqlite")
    db.init_replay_db(rep)
    yield main, rep
    main.close()
    rep.close()


def test_slot_mapping():
    assert [slot_for_player_id(i) for i in (0, 4, 5, 9)] == [0, 4, 128, 132]


def test_download_streams_and_decompresses(tmp_path):
    payload = b"PBDEMS2\x00" + b"x" * 3_000_000
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=bz2.compress(payload)))
    with httpx.Client(transport=transport) as client:
        out = download_replay("http://replay1.valve.net/570/1_2.dem.bz2", tmp_path / "1.dem", client=client)
    assert out.read_bytes() == payload
    assert not (tmp_path / "1.part").exists()


def test_name_table_interns(conns):
    _, rep = conns
    names = NameTable(rep)
    a = names.id("npc_dota_hero_axe")
    assert names.id("npc_dota_hero_axe") == a
    assert NameTable(rep).id("npc_dota_hero_axe") == a
    assert names.id(None) is None


def test_tick_sink_batches_and_tracks_minutes(conns):
    _, rep = conns
    sink = HeroTickSink(rep, 1, batch_size=10)
    for t in range(900, 900 + 30 * 125, 30):
        sink.append(snap(0, t))
    sink.flush()
    assert sink.rows == len(range(900, 900 + 30 * 125, 30))
    assert set(sink.minutes[0]) == {0, 1, 2}
    x, y, mana = rep.execute("SELECT x, y, mana FROM hero_ticks LIMIT 1").fetchone()
    assert (x, y, mana) == (-6001, -5000, 300)


def test_combat_sink_keeps_only_hero_entries(conns):
    _, rep = conns
    sink = CombatLogSink(rep, 1, NameTable(rep))
    sink(entry("DAMAGE", 2000, HEROES[0], HEROES[1]))
    sink(entry("DAMAGE", 2001, "creep_a", "creep_b", attacker_hero=False, target_hero=False))
    sink(entry("DEATH", 3000, HEROES[0], HEROES[1], will_reincarnate=True))
    sink.flush()
    assert sink.rows == 2
    assert sink.deaths == []  # aegis/reincarnation is not a death


@pytest.mark.parametrize("sample_ticks", [1, 30])
def test_parse_replay_writes_ticks_combat_and_summary(conns, tmp_path, sample_ticks):
    main, rep = conns
    dem = tmp_path / "42.dem"
    dem.write_bytes(b"x" * 1000)
    stats = parse_replay(dem, 42, rep, main, sample_ticks=sample_ticks, parse_fn=fake_parse)

    expected_ticks = 2 * len(range(900, 900 + 30 * 180 + 1, sample_ticks))
    assert stats["tick_rows"] == expected_ticks
    assert stats["combat_rows"] == 3
    assert rep.execute("SELECT COUNT(*) FROM hero_ticks WHERE match_id=42").fetchone()[0] == expected_ticks
    assert rep.execute("SELECT sample_ticks FROM replay_parses WHERE match_id=42").fetchone()[0] == sample_ticks

    # summary lands in the main DB so the momentum endpoints work
    m = main.execute("SELECT parsed_at, replay_parsed_at FROM matches WHERE match_id=42").fetchone()
    assert m["parsed_at"] and m["replay_parsed_at"]
    assert main.execute("SELECT COUNT(*) FROM player_minutes WHERE match_id=42").fetchone()[0] == 2 * 4
    kinds = {(r["player_slot"], r["kind"]) for r in main.execute("SELECT * FROM events WHERE match_id=42")}
    assert kinds == {(0, "kill"), (128, "death")}

    # re-parsing replaces rather than duplicates
    parse_replay(dem, 42, rep, main, sample_ticks=sample_ticks, parse_fn=fake_parse)
    assert rep.execute("SELECT COUNT(*) FROM combat_log WHERE match_id=42").fetchone()[0] == 3


def test_replay_does_not_overwrite_opendota_minutes(conns, tmp_path):
    from dota.demo import HERO_NAMES, make_match
    from dota.ingest import store_match

    main, rep = conns
    m = make_match(match_id=42)
    store_match(main, m, hero_names=HERO_NAMES)
    before = main.execute("SELECT COUNT(*) FROM player_minutes WHERE match_id=42").fetchone()[0]
    dem = tmp_path / "42.dem"
    dem.write_bytes(b"x")
    parse_replay(dem, 42, rep, main, sample_ticks=30, parse_fn=fake_parse)
    assert main.execute("SELECT COUNT(*) FROM player_minutes WHERE match_id=42").fetchone()[0] == before
    assert main.execute("SELECT replay_parsed_at FROM matches WHERE match_id=42").fetchone()[0]


def test_process_match_records_failure_and_cleans_up(conns, tmp_path, monkeypatch):
    import dota.replays as r

    main, rep = conns
    monkeypatch.setattr(r, "REPLAY_DIR", tmp_path / "tmp")

    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return dest

    def broken(*a):
        raise ValueError("corrupt replay")

    out = process_match(7, "http://x/7.dem.bz2", main, rep, download=download, parse_fn=broken)
    assert "corrupt" in out["error"]
    assert not (tmp_path / "tmp" / "7.dem").exists()
    assert "corrupt" in rep.execute("SELECT error FROM replay_parses WHERE match_id=7").fetchone()[0]

    ok = process_match(8, "http://x/8.dem.bz2", main, rep, download=download, parse_fn=fake_parse, sample_ticks=30)
    assert ok["tick_rows"] > 0


def test_pending_replays_skips_done(conns):
    main, rep = conns
    with main:
        for mid in (1, 2, 3):
            main.execute(
                "INSERT INTO matches (match_id, start_time, duration_s, radiant_win, replay_url) VALUES (?,?,?,?,?)",
                (mid, mid, 100, 0, f"http://x/{mid}.dem.bz2" if mid != 3 else None),
            )
    with rep:
        rep.execute("INSERT INTO replay_parses (match_id, parsed_at, sample_ticks) VALUES (2, 1, 1)")
    assert pending_replays(main, rep, 10) == [(1, "http://x/1.dem.bz2")]


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE matches (match_id INTEGER PRIMARY KEY, start_time INTEGER NOT NULL, "
              "duration_s INTEGER NOT NULL, patch INTEGER, avg_rank_tier REAL, bracket TEXT, "
              "radiant_win INTEGER NOT NULL, parsed_at INTEGER, parse_requested_at INTEGER)")
    c.close()
    conn = db.connect(path)
    db.init_db(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(matches)")}
    assert {"replay_url", "replay_parsed_at"} <= cols


def test_gem_hooks_we_rely_on_exist():
    """Guards the gem-dota internals the streaming parse depends on."""
    gem = pytest.importorskip("gem")
    from gem.extractors.players import PlayerExtractor
    from gem.parser import ReplayParser

    ext = PlayerExtractor(sample_interval=1, minute_snapshots=False)
    assert isinstance(ext.snapshots, list)
    assert callable(getattr(ReplayParser, "on_combat_log_entry", None))
    assert gem.__version__.startswith("0.14")


@pytest.mark.skipif(not __import__("os").environ.get("DOTA_TEST_REPLAY"), reason="set DOTA_TEST_REPLAY=path/to.dem")
def test_real_replay(conns):
    import os

    main, rep = conns
    stats = parse_replay(Path(os.environ["DOTA_TEST_REPLAY"]), 1, rep, main, sample_ticks=30)
    assert stats["tick_rows"] > 10_000 and stats["combat_rows"] > 1_000


def test_empty_parse_is_an_error_and_keeps_nothing(conns, tmp_path):
    from dota.replays import ReplayParseError

    main, rep = conns
    dem = tmp_path / "9.dem"
    dem.write_bytes(b"not a replay")
    with pytest.raises(ReplayParseError):
        parse_replay(dem, 9, rep, main, parse_fn=lambda *a: None)
    assert main.execute("SELECT COUNT(*) FROM matches WHERE match_id=9").fetchone()[0] == 0
    assert rep.execute("SELECT COUNT(*) FROM replay_parses WHERE match_id=9").fetchone()[0] == 0
