"""/nfl predict, simulate, power, player, report: the NFL model's projections and how well it's doing."""
from __future__ import annotations

import asyncio
import random

import discord
from discord import app_commands
from discord.ext import commands

from .. import predictions
from ..digest import matchup_line
from ..models import gamesim as gs
from ..models import nflverse as nv
from ..models.players import LABELS
from ..names import resolve

STAT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in LABELS.items()]
ROUND1 = {"pass_yds", "rush_yds", "rec_yds"}


class NFL(commands.GroupCog, group_name="nfl", group_description="NFL projections from ScoreBot's model"):
    def __init__(self, bot):
        self.bot = bot

    async def _ready(self, interaction: discord.Interaction) -> bool:
        if self.bot.model and self.bot.model.ready:
            return True
        await interaction.followup.send("🤖 The model is still loading its data (first start takes a minute). "
                                        "Try again shortly.")
        return False

    # ---------------------------------------------------------- /nfl predict
    @app_commands.command(name="predict", description="Win chances, margin and score projections")
    @app_commands.describe(team="Just one team's next game, with the detail (e.g. BUF, Bills)")
    async def predict(self, interaction: discord.Interaction, team: str | None = None):
        await interaction.response.defer()
        if not await self._ready(interaction):
            return
        svc = self.bot.model
        if not team:
            preds = [p for g in svc.upcoming() if (p := svc.predict_game(g))]
            if not preds:
                return await interaction.followup.send("No upcoming games on the schedule.")
            e = discord.Embed(title=f"🏈 Week {preds[0]['week']} projections", color=0x1ABC9C,
                              description="\n".join(matchup_line(p) for p in preds)[:4000])
            e.set_footer(text="Use /nfl predict team:<team> for the full breakdown")
            return await interaction.followup.send(embed=e)

        full = resolve(team)
        code = nv.NAME_TO_CODE.get(full or "")
        game = svc.next_game(code) if code else None
        if not game:
            return await interaction.followup.send(f"Couldn't find an upcoming game for **{team}**.")
        p = svc.predict_game(game)
        e = discord.Embed(title=f"🏈 {p['away_name']} @ {p['home_name']}",
                          description=f"Week {p['week']} · {p['gameday']}", color=0x1ABC9C)
        e.add_field(name="Win chance", inline=False,
                    value=f"{p['home']} **{p['p_home']:.0%}** · {p['away']} **{1 - p['p_home']:.0%}** "
                          f"(Elo alone: {p['home']} {p['p_elo']:.0%})")
        fav = p["home"] if p["margin"] >= 0 else p["away"]
        e.add_field(name="Projected score", inline=False,
                    value=f"{p['away']} {p['away_pts']:.0f} – {p['home_pts']:.0f} {p['home']} "
                          f"· {fav} by {abs(p['margin']):.1f} · total {p['total']:.0f} (±{p['total_sd']:.0f})")
        e.add_field(name="Why", inline=False,
                    value=f"Elo gap {p['elo_diff']:+.0f} (incl. home field) · "
                          f"net EPA/play gap {p['epa_diff']:+.3f}")
        qbs = []
        for side in ("away", "home"):
            adj, name = p[f"{side}_qb_adj"], p[f"{side}_qb_name"]
            if abs(adj) >= 0.5:
                qbs.append(f"⚠️ {p[side]}: **{name}** instead of {p[f'{side}_usual_name']} → {adj:+.1f} pts "
                           f"({p[f'{side}_qb_src']})")
            else:
                qbs.append(f"{p[side]}: {name} ({p[f'{side}_qb_src']})")
        e.add_field(name="Quarterbacks", value="\n".join(qbs), inline=False)
        await interaction.followup.send(embed=e)

    # --------------------------------------------------------- /nfl simulate
    @app_commands.command(name="simulate", description="Play a team's next game thousands of times: outcomes and player ranges")
    @app_commands.describe(team="Team (e.g. BUF, Bills)", runs="How many games to play (1,000-100,000, default 20,000)")
    async def simulate(self, interaction: discord.Interaction, team: str,
                       runs: app_commands.Range[int, 1000, gs.MAX_RUNS] = 20000):
        await interaction.response.defer()
        if not await self._ready(interaction):
            return
        svc = self.bot.model
        code = nv.NAME_TO_CODE.get(resolve(team) or "")
        game = svc.next_game(code) if code else None
        if not game:
            return await interaction.followup.send(f"Couldn't find an upcoming game for **{team}**.")
        sg = gs.build_game(svc, game)
        if sg is None:
            return await interaction.followup.send("No model prediction for that game yet.")
        seed = random.randrange(1, 10 ** 6)
        s = await asyncio.to_thread(gs.simulate, sg, runs, seed)
        await interaction.followup.send(embed=sim_embed(sg, s, game), view=SimView(s))

    # ------------------------------------------------------------ /nfl power
    @app_commands.command(name="power", description="Power rankings: Elo + EPA per play")
    async def power(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await self._ready(interaction):
            return
        rows = self.bot.model.ratings()
        lines = [f"`{i:>2}` **{t['code']:<3}** {t['power']:>5.0f} · Elo {t['elo']:.0f} · "
                 f"off {t['epa_off']:+.3f} / def {t['epa_def']:+.3f}" for i, t in enumerate(rows, 1)]
        e = discord.Embed(title="📊 NFL power rankings", description="\n".join(lines), color=0x9B59B6)
        last = self.bot.model.state.last_completed
        e.set_footer(text=f"Through {last[0]} week {last[1]} · def = EPA allowed (lower is better)" if last else "")
        await interaction.followup.send(embed=e)

    # ----------------------------------------------------------- /nfl player
    @app_commands.command(name="player", description="A player's projected stats for his next game")
    @app_commands.describe(name="Player name (e.g. Josh Allen, Bijan Robinson)", stat="Just one stat")
    @app_commands.choices(stat=STAT_CHOICES)
    async def player(self, interaction: discord.Interaction, name: str,
                     stat: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        if not await self._ready(interaction):
            return
        r = self.bot.model.player_projection(name, stat.value if stat else None)
        if not r or not r["rows"]:
            return await interaction.followup.send(f"No projection for **{name}** (not enough recent games?).")
        where = f"{'vs' if r['home'] else '@'} {r['opp']}" if r["opp"] else "no game scheduled"
        e = discord.Embed(title=f"👤 {r['name']} ({r['team']} {r['pos']})",
                          description=f"Next game: Week {r['week']} {where}" if r["week"] else where,
                          color=0xE67E22)
        for row in r["rows"]:
            fmt = (lambda v: f"{v:.0f}") if row["stat"] in ROUND1 else (lambda v: f"{v:.1f}")
            matchup = row["opp_factor"] - 1
            note = f" · defense {matchup:+.0%} vs avg" if abs(matchup) >= 0.02 else ""
            e.add_field(name=LABELS[row["stat"]], inline=False,
                        value=f"**{fmt(row['proj'])}** (80% range {row['lo']:.0f}–{row['hi']:.0f}){note}")
        if r["team_pts"]:
            e.set_footer(text=f"Team projected for {r['team_pts']:.0f} pts · weighted recent form x matchup x game script")
        await interaction.followup.send(embed=e)

    # ----------------------------------------------------------- /nfl report
    @app_commands.command(name="report", description="How accurate is the model? Backtest + live track record")
    async def report(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not await self._ready(interaction):
            return
        st = self.bot.model.state
        bt = st.backtest
        e = discord.Embed(title="🧪 Model report", color=0x2ECC71)
        if bt.get("accuracy"):
            a, ll = bt["accuracy"], bt["log_loss"]
            e.add_field(name=f"Backtest · {', '.join(map(str, bt['seasons']))} ({bt['games']} games, unseen in training)",
                        inline=False,
                        value=f"Winners picked: **{a['model']:.1%}** model · {a['elo']:.1%} Elo alone · "
                              f"{a['home']:.1%} always-home\n"
                              f"Log loss (lower = better): **{ll['model']:.4f}** · {ll['elo']:.4f} · {ll['home']:.4f}")
        if bt.get("calibration"):
            rows = [f"{c['lo']:.0%}–{c['hi']:.0%}: said {c['pred']:.0%}, happened {c['actual']:.0%} (n={c['n']})"
                    for c in bt["calibration"]]
            e.add_field(name="Calibration (favourite's win chance)", value="\n".join(rows), inline=False)
        if bt.get("totals"):
            t = bt["totals"]
            e.add_field(name="Game totals", inline=False,
                        value=f"Avg error {t['rmse_model']:.1f} pts vs {t['rmse_average']:.1f} guessing the league average")
        players = self.bot.model.players
        if players and players.backtest:
            rows = [f"{LABELS[s]}: off by {b['mae_model']:.1f} (plain average {b['mae_naive']:.1f})"
                    for s, b in players.backtest.items()]
            e.add_field(name="Player projections (last season)", value="\n".join(rows), inline=False)
        live = await predictions.track_record(self.bot.db)
        e.add_field(name="Live track record (logged before kickoff)", inline=False,
                    value=(f"{live['correct']}/{live['decided']} winners ({live['accuracy']:.0%}) · "
                           f"avg margin miss {live['margin_mae']:.1f} · total miss {live['total_mae']:.1f}"
                           if live.get("games") else "No graded games yet. Predictions are logged every 6 hours."))
        await interaction.followup.send(embed=e)


# ------------------------------------------------------------------ /nfl simulate output
POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}
MAIN_STATS = {"QB": ("pass_yds", "pass_td"), "RB": ("rush_yds", "td"),
              "WR": ("rec_yds", "rec", "td"), "TE": ("rec_yds", "rec", "td")}


def _rng(row: dict) -> str:
    q = row["q"]
    return f"{row['mean']:.0f} ({q[0]:.0f}-{q[4]:.0f})"


def player_line(name: str, p: dict) -> str:
    bits = []
    for k in MAIN_STATS.get(p["pos"], ("rec_yds", "rec", "td")):
        if k not in p:
            continue
        if k == "td":
            bits.append(f"TD {p[k]['p_any']:.0%}")
        elif k in ("pass_td", "rec"):
            bits.append(f"{p[k]['mean']:.1f} {gs.STAT_LABEL[k]}")
        else:
            bits.append(f"{_rng(p[k])} {gs.STAT_LABEL[k]}")
    return f"**{name}** ({p['pos']}): " + ", ".join(bits)


def sim_embed(sg: gs.SimGame, s: dict, game: dict) -> discord.Embed:
    h, a = sg.home, sg.away
    e = discord.Embed(title=f"🎲 {a} @ {h} · {s['n']:,} simulated games",
                      description=f"Week {game['week']} · {game.get('gameday') or ''}", color=0x8E44AD)
    e.add_field(name="Win chance", inline=False,
                value=f"{h} **{s['home_win']:.1%}** · {a} **{s['away_win']:.1%}** · went to OT {s['ot']:.1%}")
    finals = ", ".join(f"{x}-{y} ({p:.1%})" for x, y, p in s["top_scores"])
    e.add_field(name="Typical final", inline=False,
                value=f"{a} {s['avg_away']:.1f} - {s['avg_home']:.1f} {h} (averages) · total {s['total_mean']:.1f} "
                      f"± {s['total_sd']:.1f}\nMost common finals ({a}-{h}): {finals}")
    b = gs.margin_buckets(s)
    e.add_field(name="How it ends", inline=False, value="\n".join(
        f"{k.replace('home', h).replace('away', a)}: {v:.0%}" for k, v in b.items() if v >= 0.005))
    for side, code in (("away", a), ("home", h)):
        rows = sorted(((n, p) for n, p in s["players"].items() if p["team"] == side),
                      key=lambda x: POS_ORDER.get(x[1]["pos"], 9))
        if rows:
            e.add_field(name=f"{code} players (average, 10-90% range)", inline=False,
                        value="\n".join(player_line(n, p) for n, p in rows)[:1024])
    if sg.notes:
        e.add_field(name="Notes", value="\n".join(sg.notes)[:1024], inline=False)
    e.set_footer(text="Each game is played drive by drive from ScoreBot's model; pick a player below for the full "
                      "breakdown. It spreads out what the model believes; it can't know more than the model does.")
    return e


def player_embed(name: str, p: dict, n: int) -> discord.Embed:
    e = discord.Embed(title=f"👤 {name} ({p['pos']}) · {n:,} simulated games", color=0x8E44AD)
    for k in gs.STATS:
        if k not in p or (p["pos"] == "QB" and k == "rush_yds"):     # QB scrambles aren't modelled yet
            continue
        r = p[k]
        q = r["q"]
        e.add_field(name=gs.STAT_LABEL[k], inline=True,
                    value=f"avg **{r['mean']:.1f}**\nmedian {q[2]:.0f}\n10-90%: {q[0]:.0f}-{q[4]:.0f}\n"
                          f"1 or more: {r['p_any']:.0%}")
    return e


class PlayerSelect(discord.ui.Select):
    def __init__(self, s: dict):
        names = sorted(s["players"], key=lambda n: (s["players"][n]["team"], POS_ORDER.get(s["players"][n]["pos"], 9)))
        opts = [discord.SelectOption(label=f"{n} ({s['players'][n]['pos']})"[:100], value=n[:100]) for n in names[:25]]
        super().__init__(placeholder="🔍 Pick a player for the full breakdown", options=opts or
                         [discord.SelectOption(label="No players", value="_")])
        self.s = s

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        if name not in self.s["players"]:
            return await interaction.response.send_message("No players in this sim.", ephemeral=True)
        await interaction.response.send_message(embed=player_embed(name, self.s["players"][name], self.s["n"]),
                                                ephemeral=True)


class SimView(discord.ui.View):
    def __init__(self, s: dict):
        super().__init__(timeout=1800)
        if s["players"]:
            self.add_item(PlayerSelect(s))


async def setup(bot):
    await bot.add_cog(NFL(bot))
