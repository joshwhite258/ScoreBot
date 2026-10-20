"""/live: a scoreboard and box score card that edits itself until the game ends."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .. import live as lv
from .. import sports as sp

log = logging.getLogger(__name__)

LIVE_CHOICES = [app_commands.Choice(name=f"{s.emoji} {s.name}", value=s.key)
                for s in sp.SPORTS.values() if s.key in lv.LIVE_SPORTS]
REFRESH_S = 30            # between edits while a game is live
PRE_REFRESH_S = 60        # between checks before kickoff
BOARD_TTL_S = 20          # cards for the same sport and day share one scoreboard call
MAX_CARDS = 8             # cards updating at once, so ESPN isn't hammered
MAX_HOURS = 6             # a card stops updating after this long, finished or not
MAX_FAILS = 10            # consecutive ESPN failures before a card gives up
COLORS = {"in": 0xE74C3C, "pre": 0x3498DB, "post": 0x95A5A6}


def render(sport: sp.Sport, g: lv.LiveGame, players: list[dict], stale: bool = False,
           footer: str | None = None) -> discord.Embed:
    """The card: score (ball next to the team with it), status, down and distance, each team's standouts."""
    body = [f"## {lv.score_line(g)}", lv.status_text(g)]
    if (down := lv.down_text(g)):
        body.append(down)
    emb = discord.Embed(title=f"{sport.emoji} {g.away} @ {g.home}" + (f" · {g.league}" if g.league else ""),
                        description="\n".join(body), color=COLORS.get(g.state, 0x95A5A6))
    if g.state != "pre":
        shown = False
        for abbr in (g.away_abbr, g.home_abbr):
            if (lines := lv.leaders(sport.key, players, abbr)):
                emb.add_field(name=abbr, value="\n".join(lines)[:1024], inline=True)
                shown = True
        if not shown:
            emb.add_field(name="Box score", value="Player stats haven't posted yet.", inline=False)
    if footer is None:
        footer = "Final" if g.state == "post" else f"Updates every {REFRESH_S}s · source: ESPN"
        if stale:
            footer = "⚠️ ESPN isn't responding — showing the last update"
    emb.set_footer(text=footer)
    emb.timestamp = datetime.now(timezone.utc)
    return emb


class StopView(discord.ui.View):
    """One button under a live card. The starter (or anyone who can manage messages) can stop it."""

    def __init__(self, cog: "Live", starter_id: int):
        super().__init__(timeout=MAX_HOURS * 3600)
        self.cog, self.starter_id = cog, starter_id

    @discord.ui.button(label="Stop updating", emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        perms = getattr(interaction.permissions, "manage_messages", False)
        if interaction.user.id != self.starter_id and not perms:
            return await interaction.response.send_message("Only whoever started this card can stop it.",
                                                           ephemeral=True)
        task = self.cog.cards.pop(interaction.message.id, None)
        if task:
            task.cancel()
        emb = interaction.message.embeds[0] if interaction.message.embeds else None
        if emb:
            emb.set_footer(text="Stopped · no longer updating")
        await interaction.response.edit_message(embed=emb, view=None)
        self.stop()


class GamePicker(discord.ui.View):
    """A dropdown of today's matching games when /live finds more than one."""

    def __init__(self, cog: "Live", sport: sp.Sport, games: list[lv.LiveGame]):
        super().__init__(timeout=120)
        self.cog, self.sport = cog, sport
        self.by_key = {f"{g.path}|{g.espn_id}": g for g in games[:25]}
        tz = cog.bot.cfg.tz
        options = []
        for key, g in self.by_key.items():
            if g.state == "in":
                icon, when = "🔴", g.detail
            elif g.state == "post":
                icon, when = "✅", "Final"
            else:
                ts = lv.epoch(g.date)
                icon = "🕒"
                when = datetime.fromtimestamp(ts, tz).strftime("%a %-I:%M %p") if ts else g.detail
            options.append(discord.SelectOption(label=f"{g.away_abbr} @ {g.home_abbr} · {when}"[:100], value=key,
                                                emoji=icon, description=(g.league or sport.name)[:100]))
        self.select = discord.ui.Select(placeholder="Pick a game", options=options)
        self.select.callback = self.picked
        self.add_item(self.select)

    async def picked(self, interaction: discord.Interaction):
        g = self.by_key[self.select.values[0]]
        await interaction.response.edit_message(content=f"Starting {g.away_abbr} @ {g.home_abbr}…", view=None)
        await self.cog.start(interaction, self.sport, g)
        self.stop()


class Live(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cards: dict[int, asyncio.Task] = {}          # message id -> its update loop
        self._boards: dict[tuple[str, str], tuple[float, list[lv.LiveGame]]] = {}

    async def cog_unload(self):
        for task in list(self.cards.values()):
            task.cancel()

    # ------------------------------------------------------------ ESPN
    async def board(self, s: sp.Sport, date: str) -> list[lv.LiveGame]:
        """One scoreboard per sport and day, reused for a few seconds by every card that needs it."""
        now = time.monotonic()
        for k, (t, _) in list(self._boards.items()):
            if now - t > 300:
                del self._boards[k]
        hit = self._boards.get((s.key, date))
        if hit and now - hit[0] < BOARD_TTL_S:
            return hit[1]
        games = await lv.scoreboard(self.bot.session, s, date)
        self._boards[(s.key, date)] = (now, games)
        return games

    async def find_games(self, s: sp.Sport, team: str | None) -> list[lv.LiveGame]:
        """Today's games (plus yesterday's still in progress), matching the team text if given."""
        today = datetime.now(self.bot.cfg.tz)
        games: dict[tuple[str, str], lv.LiveGame] = {}
        for day, only_live in ((today - timedelta(days=1), True), (today, False)):
            for g in await self.board(s, day.strftime("%Y%m%d")):
                if (g.state == "in" or not only_live) and g.involves(team or ""):
                    games[(g.path, g.espn_id)] = g
        return lv.sort_games(list(games.values()))

    async def refresh(self, s: sp.Sport, last: lv.LiveGame) -> lv.LiveGame:
        date = lv.espn_date(last.date, datetime.now(self.bot.cfg.tz))
        for g in await self.board(s, date):
            if g.espn_id == last.espn_id and g.path == last.path:
                return g
        raise LookupError(f"game {last.espn_id} not on the {date} scoreboard")

    async def players(self, g: lv.LiveGame, previous: list[dict]) -> list[dict]:
        if g.state == "pre":
            return []
        try:
            return await lv.box(self.bot.session, g.path, g.espn_id)
        except Exception as e:
            log.warning("Box score for %s failed: %s", g.espn_id, e)
            return previous

    # ------------------------------------------------------------ card
    async def start(self, interaction: discord.Interaction, s: sp.Sport, g: lv.LiveGame):
        if len(self.cards) >= MAX_CARDS:
            return await interaction.followup.send(
                f"{MAX_CARDS} live cards are already running. Try again when one finishes.", ephemeral=True)
        if interaction.channel is None:
            return await interaction.followup.send("Live cards need a channel I can post in.", ephemeral=True)
        players = await self.players(g, [])
        view = StopView(self, interaction.user.id)
        try:
            msg = await interaction.channel.send(embed=render(s, g, players), view=view)
        except discord.HTTPException as e:
            log.warning("Couldn't post a live card: %s", e)
            return await interaction.followup.send("I can't post in this channel. Check my permissions.",
                                                   ephemeral=True)
        if g.state == "post" or g.canceled:         # nothing left to follow
            await msg.edit(view=None)
            return await interaction.followup.send("That game is already over, so the card won't update.",
                                                   ephemeral=True)
        self.cards[msg.id] = asyncio.create_task(self.run(msg, s, g, players, view))
        await interaction.followup.send(f"✅ Live card posted. It updates every {REFRESH_S}s until the game ends.",
                                        ephemeral=True)

    async def run(self, msg: discord.Message, s: sp.Sport, g: lv.LiveGame, players: list[dict], view: StopView):
        """Edit the card until the game is over, the card is deleted, ESPN keeps failing, or time runs out."""
        deadline = time.monotonic() + MAX_HOURS * 3600
        fails = 0
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(REFRESH_S if g.state == "in" else PRE_REFRESH_S)
                stale = False
                try:
                    cur = await self.refresh(s, g)
                    players = await self.players(cur, players)
                    fails = 0
                except Exception as e:
                    fails += 1
                    log.warning("Live refresh failed (%d): %s", fails, e)
                    if fails >= MAX_FAILS:
                        break
                    cur, stale = g, True
                done = cur.state == "post" or cur.canceled
                try:
                    await msg.edit(embed=render(s, cur, players, stale=stale), view=None if done else view)
                except (discord.NotFound, discord.Forbidden):
                    return                                       # the card was deleted, or I lost access
                except discord.HTTPException as e:
                    log.warning("Live card edit failed: %s", e)
                    continue
                g = cur
                if done:
                    return
            try:        # timed out, or ESPN kept failing
                await msg.edit(embed=render(s, g, players, footer="Stopped · no longer updating"), view=None)
            except discord.HTTPException:
                pass
        finally:
            self.cards.pop(msg.id, None)
            view.stop()

    # --------------------------------------------------------- command
    @app_commands.command(name="live", description="A live scoreboard and box score that updates itself")
    @app_commands.describe(sport="Which sport", team="Team to follow (leave empty to pick from today's games)")
    @app_commands.choices(sport=LIVE_CHOICES)
    async def live(self, interaction: discord.Interaction, sport: app_commands.Choice[str], team: str | None = None):
        await interaction.response.defer(ephemeral=True)
        s = sp.get(sport.value)
        try:
            games = await self.find_games(s, team)
        except Exception as e:
            log.warning("Live game lookup failed: %s", e)
            return await interaction.followup.send("ESPN isn't answering right now. Try again in a minute.",
                                                   ephemeral=True)
        if not games:
            return await interaction.followup.send(
                f"No {s.name} games found today" + (f" for **{team}**." if team else "."), ephemeral=True)
        if len(games) == 1:
            return await self.start(interaction, s, games[0])
        extra = f"\nShowing the first 25 of {len(games)}. Add `team:` to narrow it." if len(games) > 25 else ""
        await interaction.followup.send(f"Which game?{extra}", view=GamePicker(self, s, games), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Live(bot))
