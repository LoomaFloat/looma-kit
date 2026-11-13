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

Долгое вычисление. Клиент может не ждать ответа: тот же запрос с заголовком
`Prefer: respond-async` площадка принимает заданием и доводит до конца сама.
Службе для этого ничего делать не нужно, пока она отвечает быстрее десяти
минут. Вычисление дольше служба принимает сама (`/slow` ниже): отвечает 202 и
адресом в `Location`, а площадка спрашивает этот адрес, пока не получит
итоговый ответ. С заданием приходят номер (`X-Looma-Job`) и ссылки на файлы
клиента (`X-Looma-Files`).

    LOOMA_SERVE_PORT=8000 python -m own_api.server
    curl -X POST 'http://127.0.0.1:8000/primes?upto=100000'
    curl -i -X POST 'http://127.0.0.1:8000/slow?seconds=5'
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
import uuid
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


#: Долгие вычисления этой службы: номер → состояние. Живут в памяти процесса;
#: служба, которой нужно пережить свой перезапуск, пишет их в `LOOMA_CACHE_DIR`.
SLOW: dict = {}
SLOW_LOCK = threading.Lock()
MAX_SLOW_S = 3600


def fetch(link: dict) -> dict:
    """Скачать файл клиента по ссылке из задания и посчитать его хеш. Файл
    идёт из хранилища площадки прямо сюда, по частям: он может быть большим."""
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(link["url"], timeout=60) as answer:
        while True:
            piece = answer.read(1 << 20)
            if not piece:
                break
            digest.update(piece)
            size += len(piece)
    return {"bytes": size, "sha256": digest.hexdigest()}


def slow(number: str) -> None:
    """Само вычисление: работа шагами по секунде. Идёт своим потоком, а о
    ходе и результате служба отвечает, когда её спрашивают."""
    state = SLOW[number]
    try:
        files = {name: fetch(link) for name, link in state["links"].items()}
        for _second in range(state["total"]):
            if state["cancelled"]:
                return
            time.sleep(1)
            state["done"] += 1
        state["result"] = {"seconds": state["total"], "files": files}
    except Exception as exc:                      # отказ тоже итог: его надо отдать
        state["error"] = f"{type(exc).__name__}: {exc}"


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
                "GET /count?to=N — числа от 1 до N по одному, по мере готовности",
                "POST /slow?seconds=N — долгое вычисление: ответ 202, итог по адресу из Location"]})
        elif url.path == "/count":
            self._count(url)
        elif url.path.startswith("/slow/"):
            self._slow_state(url.path[len("/slow/"):])
        else:
            self._json(404, {"error": "нет такого адреса; список: GET /"})

    def do_DELETE(self) -> None:                  # noqa: N802
        """Площадка сообщает, что клиент снял задание: считать дальше незачем."""
        state = SLOW.get(urlsplit(self.path).path[len("/slow/"):])
        if state is not None:
            state["cancelled"] = True
        self._json(200 if state is not None else 404, {"cancelled": state is not None})

    def do_POST(self) -> None:                    # noqa: N802
        url = urlsplit(self.path)
        if url.path == "/hash":
            data = self._body()
            # Тело может быть любым: служба получает его байт в байт.
            self._json(200, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        elif url.path == "/primes":
            self._primes(url)
        elif url.path == "/slow":
            self._slow(url)
        else:
            self._body()
            self._json(404, {"error": "нет такого адреса; список: GET /"})

    def _slow(self, url) -> None:
        """Принять долгое вычисление: начать его и сразу ответить, где спросить итог."""
        self._body()
        try:
            seconds = int(parse_qs(url.query).get("seconds", ["30"])[0])
        except ValueError:
            seconds = 0
        if not 1 <= seconds <= MAX_SLOW_S:
            self._json(422, {"error": f"seconds: целое от 1 до {MAX_SLOW_S}"})
            return
        try:
            links = json.loads(self.headers.get("X-Looma-Files") or "{}")
        except ValueError:
            links = {}
        # Номер задания площадки служит и номером вычисления: если площадка
        # пришлёт тот же запрос ещё раз, второе вычисление не начнётся.
        number = self.headers.get("X-Looma-Job") or uuid.uuid4().hex
        with SLOW_LOCK:
            if number not in SLOW:
                SLOW[number] = {"total": seconds, "done": 0, "links": links, "result": None,
                                "error": "", "cancelled": False}
                threading.Thread(target=slow, args=(number,), daemon=True).start()
        self._json(202, {"accepted": number}, Location=f"/slow/{number}", X_Looma_Progress="0")

    def _slow_state(self, number: str) -> None:
        state = SLOW.get(number)
        if state is None:
            self._json(404, {"error": "такого вычисления нет: служба перезапускалась"})
        elif state["error"]:
            self._json(500, {"error": state["error"]})
        elif state["result"] is None:
            # Ещё считается: 202, сколько сделано и когда спросить снова.
            percent = 100 * state["done"] // state["total"]
            self._json(202, {"done": state["done"], "total": state["total"]},
                       X_Looma_Progress=str(percent), Retry_After="2")
        else:
            # Итог: обычный ответ. Своя единица счёта здесь секунды работы.
            self._json(200, state["result"], X_Looma_Units=str(state["total"]))

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
