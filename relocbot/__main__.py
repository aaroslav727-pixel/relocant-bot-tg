"""Запуск: python -m relocbot  (токен берётся из BOT_TOKEN или файла .env)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .bot import Bot, Config
from .data import load_catalog
from .store import Store
from .tg import TelegramAPI


def load_env_file(path: str = ".env") -> None:
    """Читает простой .env (KEY=VALUE). Уже заданные переменные окружения не перезаписываются."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_env_file()
    cfg = Config.from_env()
    if not cfg.token:
        sys.exit("Не задан BOT_TOKEN. Получите токен у @BotFather и положите его в .env (см. .env.example).")
    try:
        api = TelegramAPI(cfg.token, timeout=cfg.poll_timeout + 10)
    except ValueError as e:
        sys.exit(str(e))
    bot = Bot(api, Store(cfg.db_path), load_catalog(), cfg)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        logging.getLogger("relocbot").info("остановлено")


if __name__ == "__main__":
    main()
