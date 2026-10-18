"""Sport registry. Adding a sport = one Sport entry pointing at its ESPN scoreboard(s)."""
from __future__ import annotations

from dataclasses import dataclass

ESPN = "https://site.api.espn.com/apis/site/v2/sports"


@dataclass(frozen=True)
class Sport:
    key: str                       # our id
    name: str                      # "Baseball"
    emoji: str
    scoreboards: tuple[tuple[str, str], ...]   # (ESPN scoreboard URL, league label)


SOCCER_LEAGUES = (("eng.1", "Premier League"), ("uefa.champions", "Champions League"), ("usa.1", "MLS"))

NFL = Sport("nfl", "Football", "🏈", ((f"{ESPN}/football/nfl/scoreboard", "NFL"),))
NBA = Sport("nba", "Basketball", "🏀", ((f"{ESPN}/basketball/nba/scoreboard", "NBA"),))
MLB = Sport("mlb", "Baseball", "⚾", ((f"{ESPN}/baseball/mlb/scoreboard", "MLB"),))
NHL = Sport("nhl", "Hockey", "🏒", ((f"{ESPN}/hockey/nhl/scoreboard", "NHL"),))
SOCCER = Sport("soccer", "Soccer", "⚽",
               tuple((f"{ESPN}/soccer/{slug}/scoreboard", label) for slug, label in SOCCER_LEAGUES))
TENNIS = Sport("tennis", "Tennis", "🎾", ((f"{ESPN}/tennis/atp/scoreboard", "ATP"),
                                          (f"{ESPN}/tennis/wta/scoreboard", "WTA")))

SPORTS: dict[str, Sport] = {s.key: s for s in (NFL, NBA, MLB, NHL, SOCCER, TENNIS)}


def get(key: str | None) -> Sport:
    return SPORTS.get((key or "nfl").lower(), NFL)
