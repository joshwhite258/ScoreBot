"""ESPN NFL injury feed: powers /injuries and tells the model when a starting QB is out."""
from __future__ import annotations

import aiohttp

from .names import norm_name

ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
ORDER = {"Out": 0, "Injured Reserve": 1, "Suspension": 1, "Doubtful": 2, "Questionable": 3}
SEVERITY = {"Out": "🔴", "Injured Reserve": "🔴", "Suspension": "🔴", "Doubtful": "🟠", "Questionable": "🟡"}


async def fetch(session: aiohttp.ClientSession) -> dict[str, dict]:
    """{normalized name: {name, team, pos, status, detail, comment, date}} for every listed player."""
    from .espn import get_json
    return parse(await get_json(session, ESPN_INJURIES))


def parse(data: dict) -> dict[str, dict]:
    out = {}
    for team in data.get("injuries", []):
        tname = team.get("displayName")
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            name = ath.get("displayName", "")
            if not name:
                continue
            d = inj.get("details", {}) or {}
            side = d.get("side") if d.get("side") not in (None, "Not Specified") else ""
            out[norm_name(name)] = {
                "name": name, "team": (ath.get("team") or {}).get("displayName") or tname,
                "pos": (ath.get("position") or {}).get("abbreviation", ""),
                "status": inj.get("status", ""),
                "detail": " ".join(x for x in (side, d.get("type", "")) if x).strip(),
                "comment": inj.get("shortComment", ""),
                "date": inj.get("date", ""),
            }
    return out


def team_report(report: dict[str, dict], team: str) -> list[dict]:
    """A team's listed players, most serious first."""
    return sorted((v for v in report.values() if v["team"] == team and v["status"] in ORDER),
                  key=lambda v: (ORDER[v["status"]], v["pos"], v["name"]))
