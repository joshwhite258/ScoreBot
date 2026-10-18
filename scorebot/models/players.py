"""NFL player stat projections from nflverse weekly player stats.

projection = player's recent per-game average (exponentially weighted, shrunk toward
             the position average)
             x opponent factor (what that defense allows vs league average)
             x game-script factor (the team's projected points from the team model)
A distribution around the projection gives an 80% range:
  yards -> normal with sd fitted from history; counts -> Poisson.
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist

from ..names import norm_name
from .nflverse import norm

_N = NormalDist()

# stat key -> (how to compute it from a stats row, distribution)
STATS = {
    "pass_yds": (lambda r: r["passing_yards"], "normal"),
    "pass_tds": (lambda r: r["passing_tds"], "poisson"),
    "rush_yds": (lambda r: r["rushing_yards"], "normal"),
    "rec_yds": (lambda r: r["receiving_yards"], "normal"),
    "receptions": (lambda r: r["receptions"], "poisson"),
    "total_tds": (lambda r: r["rushing_tds"] + r["receiving_tds"], "poisson"),
}
LABELS = {"pass_yds": "Passing yards", "pass_tds": "Passing TDs", "rush_yds": "Rushing yards",
          "rec_yds": "Receiving yards", "receptions": "Receptions", "total_tds": "Rush + rec TDs"}
POSITION_STATS = {"QB": ("pass_yds", "pass_tds", "rush_yds"),
                  "RB": ("rush_yds", "rec_yds", "receptions", "total_tds"),
                  "WR": ("rec_yds", "receptions", "total_tds"),
                  "TE": ("rec_yds", "receptions", "total_tds")}
NUM = ("passing_yards", "passing_tds", "rushing_yards", "rushing_tds", "receptions",
       "receiving_yards", "receiving_tds", "attempts", "carries", "targets")


@dataclass
class PlayerParams:
    decay: float = 0.85          # per-game decay of a player's history
    season_carry: float = 0.5    # weight kept for last season's games
    prior_games: float = 1.0     # shrink toward position mean
    opp_decay: float = 0.9
    opp_shrink: float = 0.3      # how much of the opponent effect to trust
    script_power: float = 0.3    # elasticity to the team's projected points
    league_pts: float = 22.5


@dataclass
class PlayerModel:
    p: PlayerParams = field(default_factory=PlayerParams)
    hist: dict[str, dict[str, list[float]]] = field(default_factory=dict)  # pid -> stat -> [sum, w]
    info: dict[str, dict] = field(default_factory=dict)                     # pid -> name, team, pos
    by_name: dict[str, list[str]] = field(default_factory=dict)             # norm name -> pids
    opp: dict[str, dict[str, list[float]]] = field(default_factory=dict)    # team -> stat -> [allowed, w]
    league: dict[str, list[float]] = field(default_factory=dict)            # stat -> [sum, w]
    pos_mean: dict[tuple[str, str], float] = field(default_factory=dict)
    sd_fit: dict[str, tuple[float, float]] = field(default_factory=dict)    # stat -> (a, b): sd = a + b*sqrt(proj)
    backtest: dict = field(default_factory=dict)
    season: int | None = None

    # ------------------------------------------------------------- building
    def _new_season(self, s: int) -> None:
        if self.season is not None and s != self.season:
            c = self.p.season_carry
            for d in list(self.hist.values()) + list(self.opp.values()):
                for v in d.values():
                    v[0] *= c
                    v[1] *= c
        self.season = s

    def _proj_raw(self, pid: str, stat: str) -> float | None:
        h = self.hist.get(pid, {}).get(stat)
        if not h or h[1] <= 0:
            return None
        pos = self.info.get(pid, {}).get("pos", "")
        prior = self.pos_mean.get((pos, stat), h[0] / h[1])
        k = self.p.prior_games
        return (h[0] + k * prior) / (h[1] + k)

    def opp_factor(self, opp: str, stat: str) -> float:
        a = self.opp.get(opp, {}).get(stat)
        lg = self.league.get(stat)
        if not a or not lg or a[1] < 1 or lg[0] <= 0:
            return 1.0
        raw = (a[0] / a[1]) / (lg[0] / lg[1])
        return 1 + self.p.opp_shrink * (raw - 1)

    def script_factor(self, team_pts: float | None, stat: str) -> float:
        if not team_pts:
            return 1.0
        power = self.p.script_power * (1.5 if stat in ("pass_tds", "total_tds") else 1.0)
        return (team_pts / self.p.league_pts) ** power

    def project(self, pid: str, stat: str, opp: str | None, team_pts: float | None) -> float | None:
        base = self._proj_raw(pid, stat)
        if base is None:
            return None
        return base * (self.opp_factor(opp, stat) if opp else 1.0) * self.script_factor(team_pts, stat)

    def update_game(self, rows: list[dict]) -> None:
        """All player rows from one week (any number of games)."""
        k, ko = self.p.decay, self.p.opp_decay
        allowed: dict[tuple[str, str], float] = {}
        for r in rows:
            pid = r["player_id"]
            key = norm_name(r["player_display_name"])
            self.info[pid] = {"name": r["player_display_name"], "team": r["team"], "pos": r["position"]}
            pids = self.by_name.setdefault(key, [])
            if pid not in pids:
                pids.append(pid)
            for s, (fn, _) in STATS.items():
                if not _relevant(r, s):
                    continue
                v = fn(r)
                h = self.hist.setdefault(pid, {}).setdefault(s, [0.0, 0.0])
                h[0], h[1] = h[0] * k + v, h[1] * k + 1
                allowed[(r["opponent_team"], s)] = allowed.get((r["opponent_team"], s), 0.0) + v
        for (opp, s), v in allowed.items():
            a = self.opp.setdefault(opp, {}).setdefault(s, [0.0, 0.0])
            a[0], a[1] = a[0] * ko + v, a[1] * ko + 1
            lg = self.league.setdefault(s, [0.0, 0.0])
            lg[0], lg[1] = lg[0] + v, lg[1] + 1

    def find(self, name: str, team: str | None = None) -> str | None:
        q = norm_name(name)
        pids = self.by_name.get(q, [])
        if not pids and len(q) >= 3:          # 'mahomes' -> unique last-name / partial match
            hits = [p for n, ps in self.by_name.items() if q in n for p in ps]
            pids = hits if len({self.info[p]["name"] for p in hits}) == 1 else []
        if team:
            on = [p for p in pids if self.info[p]["team"] == team]
            pids = on or pids
        return pids[-1] if pids else None

    def sd(self, stat: str, proj: float) -> float:
        a, b = self.sd_fit.get(stat, (10.0, 4.0))
        return max(a + b * math.sqrt(max(proj, 0)), 5.0)

    def interval(self, stat: str, proj: float, coverage: float = 0.8) -> tuple[float, float]:
        """Central range expected to contain the real result `coverage` of the time."""
        lo_q, hi_q = (1 - coverage) / 2, 1 - (1 - coverage) / 2
        if STATS[stat][1] == "poisson":
            lam, cdf, k, lo = max(proj, 1e-6), 0.0, 0, None
            while True:
                cdf += math.exp(-lam) * lam ** k / math.factorial(k)
                if lo is None and cdf >= lo_q:
                    lo = k
                if cdf >= hi_q or k > 60:
                    return float(lo or 0), float(k)
                k += 1
        z = _N.inv_cdf(hi_q)
        sd = self.sd(stat, proj)
        return max(proj - z * sd, 0.0), proj + z * sd


def _relevant(r: dict, stat: str) -> bool:
    """Only count games where the player had a real role for that stat."""
    if stat.startswith("pass"):
        return r["attempts"] >= 10
    if stat == "rush_yds":
        return r["carries"] >= 3
    if stat in ("rec_yds", "receptions"):
        return r["targets"] >= 2
    if stat == "total_tds":
        return r["carries"] + r["targets"] >= 3
    return True


# ------------------------------------------------------------------ loading
def load_player_weeks(folder: Path, seasons: list[int]) -> list[dict]:
    out = []
    for s in seasons:
        path = folder / f"stats_player_week_{s}.csv"
        if not path.exists():
            continue
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                row = {"player_id": r["player_id"], "player_display_name": r["player_display_name"],
                       "position": r["position"], "team": norm(r["team"]),
                       "opponent_team": norm(r["opponent_team"]), "season": int(r["season"]),
                       "week": int(r["week"]), "game_id": r["game_id"],
                       "season_type": r["season_type"]}
                for c in NUM:
                    try:
                        row[c] = float(r.get(c) or 0)
                    except ValueError:
                        row[c] = 0.0
                out.append(row)
    return out


def build_players(rows: list[dict], params: PlayerParams | None = None,
                  test_season: int | None = None) -> PlayerModel:
    """Walk week by week. Before each week, project every player and record errors
    (for the backtest and the error-range fit); then update with that week's results."""
    pm = PlayerModel(params or PlayerParams())
    weeks: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        weeks.setdefault((r["season"], r["week"]), []).append(r)

    errs: dict[str, list[tuple[float, float]]] = {s: [] for s in STATS}       # (proj, actual)
    test: dict[str, list[tuple[float, float]]] = {s: [] for s in STATS}
    naive: dict[str, list[tuple[float, float]]] = {s: [] for s in STATS}
    for (season, week) in sorted(weeks):
        pm._new_season(season)
        wk = weeks[(season, week)]
        pm.pos_mean.update(_pos_means(pm))
        for r in wk:
            for s, (fn, _) in STATS.items():
                if not _relevant(r, s):
                    continue
                pr = pm.project(r["player_id"], s, r["opponent_team"], None)
                h = pm.hist.get(r["player_id"], {}).get(s)
                if pr is None or not h or h[1] < 2:
                    continue
                errs[s].append((pr, fn(r)))
                if test_season and season == test_season:
                    test[s].append((pr, fn(r)))
                    naive[s].append((h[0] / h[1], fn(r)))     # plain weighted average, no adjustments
        pm.update_game(wk)

    # sd = a + b*sqrt(proj), fit on absolute residuals (normal: E|e| = sd*sqrt(2/pi))
    from .ratings import fit_ols
    for s, (_, dist) in STATS.items():
        e = errs[s]
        if dist == "normal" and len(e) > 100:
            X = [[1.0, math.sqrt(max(p, 0))] for p, _ in e]
            y = [abs(a - p) * math.sqrt(math.pi / 2) for p, a in e]
            pm.sd_fit[s] = tuple(fit_ols(X, y))
    if test_season:
        pm.backtest = {s: {"n": len(test[s]),
                           "mae_model": sum(abs(a - p) for p, a in test[s]) / len(test[s]),
                           "mae_naive": sum(abs(a - p) for p, a in naive[s]) / len(naive[s])}
                       for s in STATS if test[s]}
    return pm


def _pos_means(pm: PlayerModel) -> dict[tuple[str, str], float]:
    acc: dict[tuple[str, str], list[float]] = {}
    for pid, by_stat in pm.hist.items():
        pos = pm.info.get(pid, {}).get("pos", "")
        for s, (total, w) in by_stat.items():
            if w > 0:
                a = acc.setdefault((pos, s), [0.0, 0])
                a[0] += total / w
                a[1] += 1
    return {k: v[0] / v[1] for k, v in acc.items() if v[1]}
