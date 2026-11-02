"""Сетевой слой проверяется на локальном фейковом HTTP-сервере: в настоящий Telegram тесты не ходят."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from relocbot.tg import TelegramAPI, TelegramError

TOKEN = "123456789:" + "A" * 35


class Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        Handler.seen.append((self.path, self.headers.get("Content-Type"), body))
        if self.path.endswith("/sendMessage"):
            self._json(200, {"ok": True, "result": {"message_id": 1}})
        elif self.path.endswith("/blocked"):
            self._json(403, {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"})
        elif self.path.endswith("/garbage"):
            raw = b"<html>bad gateway</html>"
            self.send_response(502)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        else:
            self._json(200, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})

    def _json(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def api():
    Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield TelegramAPI(TOKEN, timeout=5, base=f"http://127.0.0.1:{server.server_port}")
    server.shutdown()
    server.server_close()


def test_success_sends_json_with_unicode(api):
    result = api.call("sendMessage", chat_id=5, text="Привет, срок истекает ✈️", reply_markup={"inline_keyboard": []})
    assert result == {"message_id": 1}
    path, content_type, body = Handler.seen[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert content_type == "application/json"
    assert body["text"] == "Привет, срок истекает ✈️" and body["reply_markup"] == {"inline_keyboard": []}


def test_api_error_carries_code_and_description(api):
    with pytest.raises(TelegramError) as e:
        api.call("sendPhoto", chat_id=1)
    assert e.value.code == 400 and "chat not found" in e.value.description


def test_http_403_json_body_is_parsed(api):
    with pytest.raises(TelegramError) as e:
        api.call("blocked")
    assert e.value.code == 403 and "blocked" in e.value.description


def test_non_json_error_page(api):
    with pytest.raises(TelegramError) as e:
        api.call("garbage")
    assert e.value.code == 502


def test_network_error_never_leaks_token():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    closed = TelegramAPI(TOKEN, timeout=2, base=f"http://127.0.0.1:{port}")
    with pytest.raises(TelegramError) as e:
        closed.call("getUpdates", offset=0)
    assert TOKEN not in str(e.value) and "127.0.0.1" not in str(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


@pytest.mark.parametrize("bad", ["", "abc", "123:short", "ключ-доступа", "123456789:" + "A" * 10])
def test_constructor_rejects_malformed_token(bad):
    with pytest.raises(ValueError) as e:
        TelegramAPI(bad)
    if bad:
        assert bad not in str(e.value)  # ошибка не повторяет введённое значение
