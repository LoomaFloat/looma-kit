"""Учебный движок: сервер, который повторяет последнее сообщение.

Показывает весь договор движка с Looma Float в одном файле:

- порт приходит в `LOOMA_SERVE_PORT`, слушать его нужно на 127.0.0.1;
- `GET /health` отвечает 200 и `{"status": "ok"}`, когда движок готов. Пока
  модель грузится, отвечайте 503: площадка не пошлёт запрос раньше времени;
- `POST /v1/chat/completions` принимает запрос в формате OpenAI и отвечает
  целиком или потоком событий. В ответе обязательно `usage`: по нему идёт
  учёт вызовов. В потоке `usage` едет в последнем событии перед `[DONE]`;
- имя модели приходит в `LOOMA_MODEL_ID`, веса (если запись их называет) в
  `LOOMA_MODEL`, поля рецепта одним JSON в `LOOMA_ENGINE_OPTIONS`, пакеты
  расширений списком в `LOOMA_EXTENSIONS`.

    LOOMA_SERVE_PORT=8000 python -m echo_engine.server
"""

from __future__ import annotations

import importlib
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = os.environ.get("LOOMA_MODEL_ID", "echo")
OPTIONS = json.loads(os.environ.get("LOOMA_ENGINE_OPTIONS") or "{}")


def load_extensions() -> list:
    """Расширения: пакеты с функцией `transform(text) -> text`. Они лежат рядом
    с пакетом движка, поэтому импортируются по имени."""
    found = []
    for name in json.loads(os.environ.get("LOOMA_EXTENSIONS") or "[]"):
        found.append(importlib.import_module(name).transform)
    return found


EXTENSIONS = load_extensions()


def tokens(text: str) -> int:
    """Счёт для `usage`. У настоящего движка это токены модели; здесь слова."""
    return len(text.split())


def answer_to(messages: list) -> str:
    said = next((m.get("content") for m in reversed(messages or [])
                 if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    text = " ".join([said] * int(OPTIONS.get("repeat") or 1))
    if OPTIONS.get("tone") == "loud":
        text = text.upper()
    for transform in EXTENSIONS:
        text = transform(text)
    return str(OPTIONS.get("prefix") or "") + text


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:        # запросы в лог задачи не пишем
        pass

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _chunk(self, data: str) -> None:
        raw = data.encode()
        self.wfile.write(f"{len(raw):X}\r\n".encode() + raw + b"\r\n")
        self.wfile.flush()

    def do_GET(self) -> None:                     # noqa: N802
        if self.path == "/health":
            self._json(200, {"status": "ok", "model": MODEL_ID})
        else:
            self._json(404, {"error": {"message": "нет такого адреса"}})

    def do_POST(self) -> None:                    # noqa: N802
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "нет такого адреса"}})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            messages = body["messages"]
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": {"message": "нужен JSON с полем messages"}})
            return
        text = answer_to(messages)
        asked = sum(tokens(str(m.get("content") or "")) for m in messages)
        usage = {"prompt_tokens": asked, "completion_tokens": tokens(text),
                 "total_tokens": asked + tokens(text)}
        head = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(time.time()),
                "model": MODEL_ID}
        if not body.get("stream"):
            self._json(200, {**head, "object": "chat.completion", "usage": usage,
                             "choices": [{"index": 0, "finish_reason": "stop",
                                          "message": {"role": "assistant", "content": text}}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = {**head, "object": "chat.completion.chunk"}
        words = text.split(" ")
        for index, word in enumerate(words):
            piece = word + (" " if index < len(words) - 1 else "")
            event = {**chunk, "choices": [{"index": 0, "delta": {"content": piece},
                                           "finish_reason": None}]}
            self._chunk(f"data: {json.dumps(event, ensure_ascii=False)}\n\n")
        last = {**chunk, "usage": usage,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        self._chunk(f"data: {json.dumps(last, ensure_ascii=False)}\n\n")
        self._chunk("data: [DONE]\n\n")
        self._chunk("")


def main() -> None:
    port = int(os.environ["LOOMA_SERVE_PORT"])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
