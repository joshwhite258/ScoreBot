"""ScoreBot client: wires the database, ESPN/nflverse feeds, NFL models, background jobs and cogs."""
from __future__ import annotations

import logging
from datetime import datetime

import aiohttp
import discord
from discord.ext import commands, tasks

from . import injuries as inj
from . import predictions
from .config import Config
from .db import DB
from .models.service import ModelService

log = logging.getLogger("scorebot")

COGS = ["scorebot.cogs.scores", "scorebot.cogs.live", "scorebot.cogs.nfl", "scorebot.cogs.injuries", "scorebot.cogs.admin"]


class ScoreBot(commands.Bot):
    def __init__(self, cfg: Config):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.session: aiohttp.ClientSession | None = None
        self.model: ModelService | None = None
        self.started_at = datetime.now(cfg.tz)

    # ------------------------------------------------------------ lifecycle
    async def setup_hook(self) -> None:
        await self.db.connect()
        self.session = aiohttp.ClientSession(headers={"User-Agent": "ScoreBot/1.0"})
        self.model = ModelService(self.cfg.data_dir / "nflverse", self.session)
        for ext in COGS:
            await self.load_extension(ext)

        # Register commands in ONE place, or Discord shows each one twice.
        if self.cfg.dev_guild_id:
            guild = discord.Object(id=self.cfg.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            log.info("Synced commands to server %s only", self.cfg.dev_guild_id)
        else:
            await self.tree.sync()
            log.info("Synced global commands (can take up to an hour to appear in new servers)")

        self.model_loop.start()
        self.injury_loop.start()
        self.digest_loop.start()

    async def close(self) -> None:
        for loop in (self.model_loop, self.injury_loop, self.digest_loop):
            loop.cancel()
        if self.session:
            await self.session.close()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("ScoreBot online as %s in %d server(s)", self.user, len(self.guilds))

    # ------------------------------------------------------- background jobs
    @tasks.loop(hours=6)
    async def model_loop(self) -> None:
        """Rebuild the NFL models from fresh nflverse data, grade finished calls, log new ones."""
        try:
            await self.model.refresh()
            if not self.model.ready:
                return
            graded = await predictions.grade(self.db, self.model.games)
            preds = [p for g in self.model.upcoming() if (p := self.model.predict_game(g))]
            await predictions.record(self.db, preds)
            if graded:
                log.info("Graded %d prediction(s)", graded)
        except Exception:
            log.exception("Model refresh failed")

    @tasks.loop(minutes=20)
    async def injury_loop(self) -> None:
        try:
            self.model.espn_injuries = await inj.fetch(self.session)
        except Exception as e:
            log.warning("Injury refresh failed: %s", e)

    @tasks.loop(minutes=30)
    async def digest_loop(self) -> None:
        """Weekly preview on the configured day/hour, once per week, in every server that set a channel."""
        cfg = self.cfg
        now = datetime.now(cfg.tz)
        if cfg.digest_hour < 0 or now.weekday() != cfg.digest_weekday or now.hour != cfg.digest_hour:
            return
        stamp = now.strftime("%G-W%V")
        if await self.db.get_meta("digest_sent") == stamp or not self.model.ready:
            return
        await self.db.set_meta("digest_sent", stamp)
        from .digest import build_digest
        embed = await build_digest(self)
        for g in self.guilds:
            ch_id = await self.db.channel(g.id)
            ch = self.get_channel(ch_id) if ch_id else None
            if ch:
                try:
                    await ch.send(embed=embed)
                except discord.HTTPException as e:
                    log.warning("Digest to %s failed: %s", g.name, e)

    @model_loop.before_loop
    @injury_loop.before_loop
    @digest_loop.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()
