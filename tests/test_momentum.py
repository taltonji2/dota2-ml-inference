from dota.momentum import detect_swings, lane_opponent, momentum_series, player_momentum, turning_points


def test_momentum_is_zero_when_even():
    rows = [{"minute": m, "gold": 100 * m, "xp": 100 * m} for m in range(5)]
    assert all(s["momentum"] == 0 for s in momentum_series(rows, rows))


def test_detect_swings_finds_drop():
    series = [{"minute": m, "momentum": v} for m, v in enumerate([0.3, 0.3, 0.3, 0.1, -0.1, -0.1])]
    swings = detect_swings(series, window=3, swing_threshold=0.15)
    assert len(swings) == 1
    assert swings[0].direction == "down"
    assert swings[0].start_minute == 2 and swings[0].end_minute == 4
    assert swings[0].change == -0.4


def test_lane_opponent_is_same_lane_enemy(loaded):
    assert lane_opponent(loaded, 1001, 0) == 128


def test_player_momentum_shows_collapse_with_deaths(loaded):
    result = player_momentum(loaded, 1001, 0)
    early = next(s for s in result["series"] if s["minute"] == 10)
    late = result["series"][-1]
    assert early["momentum"] > 0.2
    assert late["momentum"] < 0
    downs = [s for s in result["swings"] if s["direction"] == "down"]
    assert downs, result["swings"]
    assert any(e["kind"] == "death" for s in downs for e in s["events"])


def test_turning_points_find_lead_change(loaded):
    result = turning_points(loaded, 1001)
    kinds = {p["kind"] for p in result["turning_points"]}
    assert "lead_change" in kinds
    lead = next(p for p in result["turning_points"] if p["kind"] == "lead_change")
    assert lead["leader"] == "dire"
