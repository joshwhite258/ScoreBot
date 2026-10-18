import logging

from .bot import ScoreBot
from .config import Config


def main() -> None:
    cfg = Config.load()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ScoreBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
