"""Rank tier helpers.

OpenDota encodes rank as a two-digit ``rank_tier``: the tens digit is the medal
(1 Herald ... 8 Immortal) and the ones digit is the star (1-5).
"""

from __future__ import annotations

MEDALS = {
    1: "Herald",
    2: "Guardian",
    3: "Crusader",
    4: "Archon",
    5: "Legend",
    6: "Ancient",
    7: "Divine",
    8: "Immortal",
}

# Four coarse classes keep every class well filled for the first model.
# Values are inclusive rank_tier bounds, used for /publicMatches min_rank/max_rank.
BRACKETS: dict[str, tuple[int, int]] = {
    "herald_crusader": (10, 35),
    "archon_legend": (40, 55),
    "ancient_divine": (60, 75),
    "immortal": (80, 85),
}


def medal_name(rank_tier: int | None) -> str | None:
    if not rank_tier:
        return None
    return MEDALS.get(rank_tier // 10)


def bracket_for(rank_tier: float | int | None) -> str | None:
    """Map a (possibly averaged) rank tier to one of the coarse brackets."""
    if rank_tier is None or rank_tier <= 0:
        return None
    medal = int(rank_tier) // 10
    if medal <= 3:
        return "herald_crusader"
    if medal <= 5:
        return "archon_legend"
    if medal <= 7:
        return "ancient_divine"
    return "immortal"
