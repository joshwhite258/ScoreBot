"""/nfl predict, power, player, report: the NFL model's projections and how well it's doing."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import predictions
from ..digest import matchup_line
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


async def setup(bot):
    await bot.add_cog(NFL(bot))
