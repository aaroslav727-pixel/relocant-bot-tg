"""Минимальный клиент Telegram Bot API на urllib (без внешних зависимостей)."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


class TelegramError(Exception):
    """Ошибка вызова API. Текст никогда не содержит токен или URL."""

    def __init__(self, method: str, description: str, code: int | None = None):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.code = code


class TelegramAPI:
    def __init__(self, token: str, timeout: float = 40.0, base: str = "https://api.telegram.org"):
        if not _TOKEN_RE.match(token):
            raise ValueError("BOT_TOKEN выглядит некорректно: возьмите токен у @BotFather")
        self._token = token
        self._base = base
        self.timeout = timeout

    def call(self, method: str, **params):
        url = f"{self._base}/bot{self._token}/{method}"
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                payload = json.load(e)
            except Exception:
                raise TelegramError(method, f"HTTP {e.code}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            # намеренно без цепочки исключений: в тексте могла бы оказаться ссылка с токеном
            raise TelegramError(method, f"сетевая ошибка ({type(e).__name__})") from None
        if not payload.get("ok"):
            raise TelegramError(method, payload.get("description", "unknown"), payload.get("error_code"))
        return payload["result"]
