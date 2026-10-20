"""/scores: today's games for any sport, live scores first."""
from __future__ import annotations

from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from .. import scores as sc
from .. import sports as sp

SPORT_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key) for s in sp.SPORTS.values()]
MAX_GAMES = 45          # a college football Saturday has 100+; an embed holds ~6000 characters
DAY_CHOICES = [app_commands.Choice(name="Today", value=0), app_commands.Choice(name="Yesterday", value=-1),
               app_commands.Choice(name="Tomorrow", value=1)]


class Scores(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="scores", description="Live scores and today's schedule")
    @app_commands.describe(sport="Which sport", team="Only games with this team or player",
                           day="Today (default), yesterday or tomorrow")
    @app_commands.choices(sport=SPORT_CHOICES, day=DAY_CHOICES)
    async def scores(self, interaction: discord.Interaction, sport: app_commands.Choice[str],
                     team: str | None = None, day: app_commands.Choice[int] | None = None):
        await interaction.response.defer()
        s = sp.get(sport.value)
        tz = self.bot.cfg.tz
        when = datetime.now(tz) + timedelta(days=day.value if day else 0)
        games = [g for g in await sc.fetch_day(self.bot.session, s, tz, when) if g.involves(team or "")]
        title = f"{s.emoji} {s.name} · {when.strftime('%a %b %-d')}"
        if not games:
            return await interaction.followup.send(f"{title}\nNo games found" + (f" for **{team}**." if team else "."))
        e = discord.Embed(title=title, color=0x3498DB)
        shown = games[:MAX_GAMES]                      # live first, so a big college slate keeps what matters
        by_league: dict[str, list[str]] = {}
        for g in shown:
            by_league.setdefault(g.league, []).append(sc.fmt_score(g, tz))
        for league, lines in by_league.items():
            for i in range(0, len(lines), 15):          # embed fields hold ~1024 chars
                e.add_field(name=league if i == 0 else f"{league} (cont.)", value="\n".join(lines[i:i + 15]),
                            inline=False)
        live = sum(g.state == "in" for g in games)
        more = f" · showing the first {len(shown)}, add team: to find the rest" if len(games) > len(shown) else ""
        e.set_footer(text=f"{len(games)} games · {live} live{more} · source: ESPN")
        await interaction.followup.send(embed=e)


async def setup(bot):
    await bot.add_cog(Scores(bot))
