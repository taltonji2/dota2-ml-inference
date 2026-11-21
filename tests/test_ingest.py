from dota.brackets import bracket_for, medal_name
from dota.ingest import event_rows, match_row, minute_rows, store_match
from dota.opendota import is_parsed

from conftest import HERO_NAMES, make_match


def test_brackets():
    assert bracket_for(15) == "herald_crusader"
    assert bracket_for(45.5) == "archon_legend"
    assert bracket_for(64) == "ancient_divine"
    assert bracket_for(80) == "immortal"
    assert bracket_for(None) is None
    assert medal_name(64) == "Ancient"


def test_match_row_averages_player_ranks_when_not_given():
    row = match_row(make_match())
    assert row["avg_rank_tier"] == 64
    assert row["bracket"] == "ancient_divine"
    assert row["parsed_at"] is not None


def test_unparsed_match_has_no_series():
    m = make_match(parsed=False)
    assert not is_parsed(m)
    assert minute_rows(m) == []
    assert match_row(m)["parsed_at"] is None


def test_minute_rows_cover_every_player_and_minute():
    rows = minute_rows(make_match(minutes=20))
    assert len(rows) == 10 * 21


def test_kills_produce_matching_deaths():
    rows = event_rows(make_match(), HERO_NAMES)
    kinds = [(r[1], r[3]) for r in rows]
    assert kinds.count((128, "kill")) == 2
    assert kinds.count((0, "death")) == 2
    assert (None, "objective") in kinds


def test_store_match_is_idempotent(conn):
    m = make_match()
    assert store_match(conn, m, hero_names=HERO_NAMES)
    store_match(conn, m, hero_names=HERO_NAMES)
    assert conn.execute("SELECT COUNT(*) FROM player_minutes").fetchone()[0] == 210
    assert conn.execute("SELECT COUNT(*) FROM match_players").fetchone()[0] == 10
