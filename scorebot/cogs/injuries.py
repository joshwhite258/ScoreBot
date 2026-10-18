"""/injuries: a team's current NFL injury report."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import injuries as inj
from ..names import resolve


class Injuries(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _report(self) -> dict:
        rep = self.bot.model.espn_injuries if self.bot.model else {}
        if not rep:
            rep = await inj.fetch(self.bot.session)
            if self.bot.model:
                self.bot.model.espn_injuries = rep
        return rep

    @app_commands.command(name="injuries", description="An NFL team's current injury report")
    @app_commands.describe(team="e.g. BUF, Bills")
    async def injuries(self, interaction: discord.Interaction, team: str):
        await interaction.response.defer()
        full = resolve(team)
        if not full:
            return await interaction.followup.send(f"Don't know team **{team}**.")
        rows = inj.team_report(await self._report(), full)
        if not rows:
            return await interaction.followup.send(f"✅ {full}: nobody listed.")
        lines = [f"{inj.SEVERITY.get(v['status'], '•')} **{v['name']}** ({v['pos']}) {v['status']}"
                 + (f" · {v['detail']}" if v["detail"] else "") for v in rows[:25]]
        e = discord.Embed(title=f"🏥 {full} injuries", description="\n".join(lines), color=0xE67E22)
        e.set_footer(text="Source: ESPN · refreshed every 20 min")
        await interaction.followup.send(embed=e)


async def setup(bot):
    await bot.add_cog(Injuries(bot))
