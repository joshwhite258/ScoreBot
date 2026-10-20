"""/scorebot help, status, setchannel, digest, update."""
from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import selfupdate

log = logging.getLogger(__name__)

HELP = """
**Scores** (NFL, college football, basketball, baseball, hockey, soccer, tennis)
`/scores` — live scores and today's schedule · add `team:` to filter, `day:` for yesterday/tomorrow
`/live` — a scoreboard and box score card that updates itself until the game ends (not tennis) · `team:` to follow one team

**NFL model**
`/nfl predict` — this week's win chances, margins and projected scores · `team:` for the full breakdown
`/nfl power` — power rankings (Elo + EPA per play)
`/nfl player` — a player's projected stats for his next game, with an 80% range
`/nfl report` — backtest accuracy, calibration and the live track record

**Injuries**
`/injuries` — an NFL team's current report

**Server**
`/scorebot setchannel` — where the weekly preview posts (needs Manage Server)
`/scorebot digest` — post the weekly preview now
`/scorebot status` — model and data freshness
`/scorebot update` — pull the latest version from GitHub and restart (owner only)
"""


class Admin(commands.GroupCog, group_name="scorebot", group_description="ScoreBot settings"):
    def __init__(self, bot):
        self.bot = bot

    @property
    def data_dir(self):
        return self.bot.cfg.data_dir

    async def cog_load(self):
        self.update_watch.start()

    async def cog_unload(self):
        self.update_watch.cancel()

    @app_commands.command(name="help", description="What ScoreBot can do")
    async def help(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(title="🏟️ ScoreBot", description=HELP, color=0x5865F2), ephemeral=True)

    @app_commands.command(name="setchannel", description="Post the weekly preview in this channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def setchannel(self, interaction: discord.Interaction):
        await self.bot.db.set_channel(interaction.guild_id, interaction.channel_id)
        await interaction.response.send_message(f"✅ Weekly previews will post in <#{interaction.channel_id}>.")

    @app_commands.command(name="digest", description="Post the weekly preview now")
    async def digest(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not (self.bot.model and self.bot.model.ready):
            return await interaction.followup.send("🤖 The model is still loading. Try again shortly.")
        from ..digest import build_digest
        await interaction.followup.send(embed=await build_digest(self.bot))

    @app_commands.command(name="status", description="Model and data freshness")
    async def status(self, interaction: discord.Interaction):
        m = self.bot.model
        lines = [f"Online since {self.bot.started_at.strftime('%b %-d %-I:%M %p')}",
                 f"Servers: {len(self.bot.guilds)}"]
        if m and m.ready:
            built = datetime.fromtimestamp(m.built_at, self.bot.cfg.tz).strftime("%b %-d %-I:%M %p")
            last = m.state.last_completed
            lines += [f"NFL model rebuilt {built} (data through {last[0]} week {last[1]})" if last else
                      f"NFL model rebuilt {built}",
                      f"Player model: {'ready' if m.players else 'unavailable'}",
                      f"Injury report: {len(m.espn_injuries)} players listed"]
        else:
            lines.append("NFL model: loading")
        await interaction.response.send_message(
            embed=discord.Embed(title="ScoreBot status", description="\n".join(lines), color=0x95A5A6), ephemeral=True)

    # ---------------------------------------------------------------- update
    @app_commands.command(name="update", description="Pull the latest ScoreBot from GitHub and restart (owner only)")
    async def update(self, interaction: discord.Interaction):
        if interaction.user.id != self.bot.cfg.owner_user_id:
            return await interaction.response.send_message("Only the bot owner can update ScoreBot.", ephemeral=True)
        state = selfupdate.pending(self.data_dir)
        if state == "running":
            return await interaction.response.send_message("⏳ An update is already running.", ephemeral=True)
        if state == "queued" and not selfupdate.stale_request(self.data_dir):
            return await interaction.response.send_message("⏳ An update is already queued.", ephemeral=True)
        selfupdate.write_request(self.data_dir, interaction.channel_id, interaction.user.id)
        await interaction.response.send_message(
            "🔄 Checking GitHub for updates. If there's a new version I'll install it, restart "
            "(about a minute) and post the result here.")

    @tasks.loop(seconds=15)
    async def update_watch(self):
        """Post the updater's result, and catch a request the root updater never picked up."""
        r = selfupdate.take_result(self.data_dir)
        if r:
            await self._post(r.get("channel_id"), selfupdate.result_message(r))
        elif selfupdate.stale_request(self.data_dir):
            selfupdate.clear_request(self.data_dir)
            await self._post(None, "⚠️ `/scorebot update` got no answer from the updater. Run "
                                   "`deploy/auto-update.sh` once on the server to install it.")

    @update_watch.before_loop
    async def _before_update_watch(self):
        await self.bot.wait_until_ready()

    async def _post(self, channel_id, text: str):
        ch = self.bot.get_channel(int(channel_id)) if channel_id else None
        if ch is None:
            for g in self.bot.guilds:
                if (cid := await self.bot.db.channel(g.id)) and (ch := self.bot.get_channel(cid)):
                    break
        if ch is None:
            log.info("Update result (no channel to post to): %s", text)
            return
        try:
            await ch.send(text)
        except discord.HTTPException as e:
            log.warning("Couldn't post update result: %s", e)


async def setup(bot):
    await bot.add_cog(Admin(bot))
