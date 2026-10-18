"""Settings loaded from environment / .env file."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    """Read a setting, dropping inline '# comments' (systemd keeps them in the value)."""
    v = os.environ.get(name, default)
    v = re.split(r"\s+#", v, maxsplit=1)[0]
    return v.strip().strip('"').strip("'")


load_dotenv(_env("SCOREBOT_ENV_FILE", ".env"))


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


@dataclass(frozen=True)
class Config:
    discord_token: str
    db_path: Path
    tz: ZoneInfo
    dev_guild_id: int | None = None
    owner_user_id: int | None = None   # can run /scorebot update
    log_level: str = "INFO"
    digest_weekday: int = 1            # 0=Mon … 1=Tue
    digest_hour: int = 10              # local hour; -1 = off

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @classmethod
    def load(cls) -> "Config":
        token = _env("DISCORD_TOKEN", "")
        if not token:
            raise SystemExit("DISCORD_TOKEN is missing — set it in .env")
        dev = _env("DEV_GUILD_ID", "")
        return cls(
            discord_token=token,
            db_path=Path(_env("DB_PATH", "data/scorebot.db")),
            tz=ZoneInfo(_env("TZ_DISPLAY", "America/New_York")),
            dev_guild_id=int(dev) if dev else None,
            owner_user_id=_int("OWNER_USER_ID", 0) or None,
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            digest_weekday=_int("DIGEST_WEEKDAY", 1),
            digest_hour=_int("DIGEST_HOUR", 10),
        )
