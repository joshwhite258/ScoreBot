"""Small async SQLite layer: per-server settings, the model's prediction log, key/value meta."""
from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS predictions (
    game_id    TEXT PRIMARY KEY,      -- nflverse game id, e.g. 2026_07_KC_BUF
    season     INTEGER NOT NULL,
    week       INTEGER NOT NULL,
    home       TEXT NOT NULL,         -- team codes
    away       TEXT NOT NULL,
    p_home     REAL NOT NULL,         -- model P(home wins), frozen before kickoff
    margin     REAL NOT NULL,         -- projected home margin
    total      REAL NOT NULL,         -- projected combined points
    created_at REAL NOT NULL,
    home_score INTEGER,               -- filled in once the game is final
    away_score INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.rowcount

    async def fetchone(self, sql: str, params: tuple = ()):
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------ settings
    async def channel(self, guild_id: int) -> int | None:
        r = await self.fetchone("SELECT channel_id FROM guild_settings WHERE guild_id=?", (guild_id,))
        return r["channel_id"] if r else None

    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        await self.execute(
            "INSERT INTO guild_settings(guild_id, channel_id) VALUES(?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id", (guild_id, channel_id))

    async def get_meta(self, key: str) -> str | None:
        r = await self.fetchone("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
