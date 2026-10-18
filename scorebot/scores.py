"""Live scores and schedules from ESPN's public scoreboards, for every registered sport.

Team sports list one competition per event. Tennis nests matches under
event.groupings[].competitions[] and uses athletes instead of teams, so the
parser walks both shapes and reads whichever name is present.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

from . import sports as sp
from .espn import EspnBlocked, get_json

log = logging.getLogger(__name__)

STATE_ORDER = {"in": 0, "pre": 1, "post": 2}


@dataclass
class Game:
    id: str
    league: str
    state: str                 # "pre", "in" or "post"
    detail: str                # ESPN's short status: "Q3 5:12", "Final", "10/19 - 1:00 PM EDT"
    start: datetime | None
    home: str
    away: str
    home_short: str
    away_short: str
    home_score: str | None
    away_score: str | None
    home_full: str = ""
    away_full: str = ""

    def involves(self, query: str) -> bool:
        q = (query or "").strip().lower()
        return not q or any(q in s.lower() for s in (self.home, self.away, self.home_short,
                                                      self.away_short, self.home_full, self.away_full))


def _name(c: dict) -> tuple[str, str, str]:
    """(display name, short label, full name) for a team or a tennis player."""
    if c.get("team"):
        t = c["team"]
        full = t.get("displayName") or t.get("name") or "?"
        return t.get("shortDisplayName") or full, t.get("abbreviation") or full, full
    a = c.get("athlete") or {}
    full = a.get("displayName") or a.get("fullName") or "?"
    return full, a.get("shortName") or full, full


def _score(c: dict) -> str | None:
    """Team score, or sets won for tennis (whose competitors carry per-set linescores instead)."""
    if c.get("score") not in (None, ""):
        return str(c["score"])
    sets = c.get("linescores") or []
    return str(sum(1 for ls in sets if ls.get("winner"))) if sets else None


def _parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _competitions(ev: dict):
    for comp in ev.get("competitions") or []:
        yield comp
    for grp in ev.get("groupings") or []:
        for comp in grp.get("competitions") or []:
            yield comp


def parse_scoreboard(data: dict, league: str) -> list[Game]:
    out = []
    for ev in data.get("events", []):
        for comp in _competitions(ev):
            cs = comp.get("competitors") or []
            if len(cs) != 2:
                continue
            by_side = {c.get("homeAway"): c for c in cs}
            home, away = by_side.get("home"), by_side.get("away")
            if not home or not away:            # tennis: no home/away, keep listed order
                away, home = cs[0], cs[1]
            status = (comp.get("status") or ev.get("status") or {}).get("type") or {}
            state = status.get("state") or "pre"
            hn, hs, hf = _name(home)
            an, as_, af = _name(away)
            out.append(Game(
                id=str(comp.get("id") or ev.get("id")), league=league, state=state,
                detail=status.get("shortDetail") or status.get("detail") or "",
                start=_parse_time(comp.get("date") or ev.get("date")),
                home=hn, away=an, home_short=hs, away_short=as_, home_full=hf, away_full=af,
                home_score=_score(home) if state != "pre" else None,
                away_score=_score(away) if state != "pre" else None))
    return out


def sort_games(games: list[Game]) -> list[Game]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(games, key=lambda g: (STATE_ORDER.get(g.state, 3), g.start or far))


async def fetch_day(session: aiohttp.ClientSession, sport: sp.Sport, tz: ZoneInfo,
                    day: datetime | None = None) -> list[Game]:
    """Every game on one local calendar day (today by default), live first."""
    d = (day or datetime.now(tz)).strftime("%Y%m%d")
    games: list[Game] = []
    for url, league in sport.scoreboards:
        try:
            games += parse_scoreboard(await get_json(session, url, {"dates": d}, timeout=15), league)
        except EspnBlocked as e:
            log.warning("ESPN scores stopped: %s", e)
            break
        except Exception as e:
            log.warning("ESPN scores %s failed: %s", league, e)
    return sort_games(games)


def fmt_score(g: Game, tz: ZoneInfo) -> str:
    """One line per game, e.g. '🔴 KC 17 – 21 BUF · Q3 5:12'."""
    if g.state == "pre":
        when = g.start.astimezone(tz).strftime("%-I:%M %p") if g.start else g.detail
        return f"🕒 {g.away_short} @ {g.home_short} · {when}"
    icon = "🔴" if g.state == "in" else "✅"
    return f"{icon} {g.away_short} {g.away_score or 0} – {g.home_score or 0} {g.home_short} · {g.detail}"
