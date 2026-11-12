"""Движок со своим API: служба, которая отвечает на свои адреса по-своему.

Это общий случай движка автора. Площадка знает о службе три вещи: где она
работает, жива ли она и сколько стоит вызов. Запросы она передаёт как есть:
метод, путь, строка запроса, тело и заголовки приходят сюда без изменений, а
статус, заголовки и тело ответа так же уходят клиенту.

Что служба обязана площадке:

- слушать порт из `LOOMA_SERVE_PORT` (процесс на петле, служба в образе на
  всех адресах);
- отвечать 200 на `GET /health`, когда готова работать.

Всё остальное её собственное. Клиент зовёт модель на этом движке так:

    POST https://api.loomafloat.ru/v1/run/<автор>/<модель>/primes?upto=1000000

и служба получает `POST /primes?upto=1000000`.

Счёт. Вызов стоит столько, сколько назначил владелец модели. Один вызов —
одна единица; служба может назвать другое число заголовком ответа
`X-Looma-Units`. Здесь поиск простых чисел считается по миллионам проверенных
чисел, остальное по вызову. Ответ со статусом 400 и выше не считается.

    LOOMA_SERVE_PORT=8000 python -m own_api.server
    curl -X POST 'http://127.0.0.1:8000/primes?upto=100000'
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MODEL_ID = os.environ.get("LOOMA_MODEL_ID", "own-api")
OPTIONS = json.loads(os.environ.get("LOOMA_ENGINE_OPTIONS") or "{}")
#: Предел поиска простых чисел: поле рецепта, его задаёт автор модели.
LIMIT = int(OPTIONS.get("limit") or 5_000_000)


def primes_upto(n: int) -> int:
    """Сколько простых чисел не больше n (решето Эратосфена)."""
    if n < 2:
        return 0
    sieve = bytearray([1]) * (n + 1)
    sieve[0:2] = b"\x00\x00"
    for value in range(2, int(n ** 0.5) + 1):
        if sieve[value]:
            sieve[value * value::value] = bytearray(len(range(value * value, n + 1, value)))
    return sum(sieve)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:        # запросы в лог задачи не пишем
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json",
              **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, body: dict, **headers: str) -> None:
        self._send(status, json.dumps(body, ensure_ascii=False).encode(), **headers)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_GET(self) -> None:                     # noqa: N802
        url = urlsplit(self.path)
        if url.path == "/health":
            self._json(200, {"status": "ok", "model": MODEL_ID})
        elif url.path == "/":
            self._json(200, {"model": MODEL_ID, "limit": LIMIT, "routes": [
                "POST /hash — sha256 присланных данных",
                "POST /primes?upto=N — сколько простых чисел не больше N",
                "GET /count?to=N — числа от 1 до N по одному, по мере готовности"]})
        elif url.path == "/count":
            self._count(url)
        else:
            self._json(404, {"error": "нет такого адреса; список: GET /"})

    def do_POST(self) -> None:                    # noqa: N802
        url = urlsplit(self.path)
        if url.path == "/hash":
            data = self._body()
            # Тело может быть любым: служба получает его байт в байт.
            self._json(200, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        elif url.path == "/primes":
            self._primes(url)
        else:
            self._body()
            self._json(404, {"error": "нет такого адреса; список: GET /"})

    def _primes(self, url) -> None:
        self._body()
        try:
            upto = int(parse_qs(url.query).get("upto", ["0"])[0])
        except ValueError:
            upto = 0
        if not 2 <= upto <= LIMIT:
            # Отказ площадка клиенту не засчитает: статус 400 и выше бесплатен.
            self._json(422, {"error": f"upto: целое от 2 до {LIMIT}"})
            return
        began = time.time()
        found = primes_upto(upto)
        # Своя единица счёта: миллион проверенных чисел, не меньше одной.
        units = max(1.0, upto / 1_000_000)
        self._json(200, {"upto": upto, "primes": found, "seconds": round(time.time() - began, 3)},
                   X_Looma_Units=f"{units:g}")

    def _count(self, url) -> None:
        """Ответ по частям: клиент видит каждую строку, как только она готова."""
        try:
            to = max(1, min(100, int(parse_qs(url.query).get("to", ["5"])[0])))
        except ValueError:
            to = 5
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for number in range(1, to + 1):
            line = f"{number}\n".encode()
            self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
            self.wfile.flush()
            time.sleep(0.2)
        self.wfile.write(b"0\r\n\r\n")


def main() -> None:
    port = int(os.environ.get("LOOMA_SERVE_PORT") or os.environ.get("PORT") or 8000)
    host = os.environ.get("OWN_API_BIND") or "127.0.0.1"
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
