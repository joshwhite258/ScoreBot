"""/form: a player's last few games and his averages (NBA, NHL, MLB)."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import form as fm
from .. import sports as sp

log = logging.getLogger(__name__)

FORM_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key)
                for s in sp.SPORTS.values() if s.key in fm.FORM_SPORTS]


def render(sport: sp.Sport, athlete: dict, data: dict, n: int) -> discord.Embed:
    who = athlete["name"] + (f" · {athlete['team']}" if athlete.get("team") else "")
    emb = discord.Embed(title=f"{sport.emoji} {who}", color=0x3498DB)
    inj = data.get("injury")
    if inj and inj[0]:
        icon = "🔴" if inj[0] in fm.OUT else "🟡"
        emb.description = f"{icon} **{inj[0]}**" + (f" · {inj[1]}" if inj[1] else "")
    games = data["games"]
    if not games:
        emb.add_field(name="Recent games", value="No finished games with a box score yet this season.",
                      inline=False)
    else:
        lines = [f"`{fm.short_date(g['date']):>6}` {g['result']} — {g['text']}" for g in games]
        emb.add_field(name=f"Last {len(games)} game{'s' if len(games) != 1 else ''}",
                      value="\n".join(lines)[:1024], inline=False)
        emb.add_field(name="Average", value=data["average"], inline=False)
    emb.set_footer(text="Source: ESPN box scores")
    return emb


class AthletePicker(discord.ui.View):
    """A dropdown when the name matches more than one player."""

    def __init__(self, cog: "Form", sport: sp.Sport, cands: list[dict], n: int):
        super().__init__(timeout=120)
        self.cog, self.sport, self.n = cog, sport, n
        self.by_id = {c["id"]: c for c in cands[:10]}
        self.select = discord.ui.Select(placeholder="Which player?", options=[
            discord.SelectOption(label=f"{c['name']} ({c['team'] or '?'})"[:100], value=c["id"],
                                 description=c["team_name"][:100] or None) for c in self.by_id.values()])
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Looking him up…", view=None)
        await self.cog.show(interaction, self.sport, self.by_id[self.select.values[0]], self.n)
        self.stop()


class Form(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def show(self, interaction: discord.Interaction, s: sp.Sport, athlete: dict, n: int):
        if not athlete.get("team"):
            return await interaction.followup.send(f"I can't tell which team **{athlete['name']}** is on right now.",
                                                   ephemeral=True)
        try:
            data = await fm.recent_form(self.bot.session, s.key, athlete, n)
        except Exception as e:
            log.warning("Player form failed for %s: %s", athlete["name"], e)
            return await interaction.followup.send("ESPN isn't answering right now. Try again in a minute.",
                                                   ephemeral=True)
        await interaction.followup.send(embed=render(s, athlete, data, n))

    @app_commands.command(name="form", description="A player's last few games and averages")
    @app_commands.describe(sport="Which sport", player="Player name, e.g. Jamal Murray or J. Murray",
                           games="How many recent games (default 5)")
    @app_commands.choices(sport=FORM_CHOICES)
    async def form(self, interaction: discord.Interaction, sport: app_commands.Choice[str], player: str,
                   games: app_commands.Range[int, 1, 10] = 5):
        await interaction.response.defer()
        s = sp.get(sport.value)
        try:
            cands = await fm.find_athletes(self.bot.session, s.key, player)
        except Exception as e:
            log.warning("Player search failed for %r: %s", player, e)
            return await interaction.followup.send("ESPN isn't answering right now. Try again in a minute.",
                                                   ephemeral=True)
        if not cands:
            return await interaction.followup.send(f"Couldn't find a {s.name.lower()} player matching **{player}**.",
                                                   ephemeral=True)
        if len(cands) == 1 or fm.norm_name(cands[0]["name"]) == fm.norm_name(player):
            return await self.show(interaction, s, cands[0], games)
        await interaction.followup.send("Which player?", view=AthletePicker(self, s, cands, games), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Form(bot))
