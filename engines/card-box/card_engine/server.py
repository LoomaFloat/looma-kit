"""Учебный движок на видеокарте: отвечает, какую карту видит.

Договор службы тот же, что у `engines/echo-box`: слушать `0.0.0.0` на порту из
`LOOMA_SERVE_PORT`, `GET /health` отвечает 200, когда движок готов,
`POST /v1/chat/completions` принимает запрос в формате OpenAI и возвращает
`usage`.

Что здесь про видеокарту:

- реплика получает одну карту, и внутри она всегда под номером 0, какой бы по
  счёту она ни была на машине. `CUDA_VISIBLE_DEVICES` площадка не ставит;
- библиотеки драйвера (`libcuda.so.1`, `libnvidia-ml.so.1`) и `nvidia-smi`
  лежат в `/usr/local/nvidia`, пути к ним уже в `LD_LIBRARY_PATH` и `PATH`;
- пока карта не ответила, `/health` отдаёт 503: площадка не пошлёт запросы
  реплике, которая не готова.

Чтобы образец остался маленьким, он говорит с драйвером напрямую. Настоящий
движок берёт PyTorch или другую библиотеку: они находят карту так же.

    LOOMA_SERVE_PORT=8000 python -m card_engine.server
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = os.environ.get("LOOMA_MODEL_ID", "card-box")
GIB = 1024 ** 3
#: Что узнали о картах: список после удачного чтения, текст ошибки после неудачи.
STATE = {"cards": None, "error": "карта ещё не опрошена"}


def read_cards() -> list:
    """Карты, которые видит реплика: имя, память всего и свободно, ГиБ."""
    cuda = ctypes.CDLL("libcuda.so.1")

    def call(name: str, *args) -> None:
        code = getattr(cuda, name)(*args)
        if code != 0:
            raise RuntimeError(f"{name}: ошибка CUDA {code}")

    call("cuInit", 0)
    count = ctypes.c_int()
    call("cuDeviceGetCount", ctypes.byref(count))
    cards = []
    for index in range(count.value):
        device = ctypes.c_int()
        call("cuDeviceGet", ctypes.byref(device), index)
        name = ctypes.create_string_buffer(128)
        call("cuDeviceGetName", name, 128, device)
        # Свободную память драйвер сообщает только внутри контекста карты.
        context = ctypes.c_void_p()
        call("cuCtxCreate_v2", ctypes.byref(context), 0, device)
        free, total = ctypes.c_size_t(), ctypes.c_size_t()
        call("cuMemGetInfo_v2", ctypes.byref(free), ctypes.byref(total))
        call("cuCtxDestroy_v2", context)
        cards.append({"index": index, "name": name.value.decode(),
                      "total_gib": round(total.value / GIB, 1),
                      "free_gib": round(free.value / GIB, 1)})
    return cards


def load() -> None:
    try:
        STATE["cards"], STATE["error"] = read_cards(), ""
    except (OSError, RuntimeError) as exc:
        STATE["error"] = f"видеокарта недоступна: {exc}"
        print(STATE["error"], flush=True)


def answer() -> str:
    cards = STATE["cards"] or []
    if not cards:
        return "Видеокарт не вижу."
    listed = "; ".join(f"{c['index']}: {c['name']}, {c['total_gib']} ГиБ, свободно "
                       f"{c['free_gib']} ГиБ" for c in cards)
    return f"Вижу видеокарт: {len(cards)}. {listed}."


def tokens(text: str) -> int:
    """Счёт для `usage`. У настоящего движка это токены модели; здесь слова."""
    return len(text.split())


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
        if self.path != "/health":
            self._json(404, {"error": {"message": "нет такого адреса"}})
        elif STATE["cards"] is None:
            self._json(503, {"status": "loading", "error": STATE["error"]})
        else:
            self._json(200, {"status": "ok", "model": MODEL_ID, "cards": STATE["cards"]})

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
        if STATE["cards"] is None:
            self._json(503, {"error": {"message": STATE["error"]}})
            return
        text = answer()
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
    port = int(os.environ.get("LOOMA_SERVE_PORT") or os.environ.get("PORT") or 8000)
    # Карту опрашиваем рядом, а порт слушаем сразу: площадка видит, что служба
    # жива и ещё грузится, а не гадает, почему порт закрыт.
    threading.Thread(target=load, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
