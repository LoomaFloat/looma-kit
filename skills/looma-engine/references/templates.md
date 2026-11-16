# Каркасы службы движка

Готовые образцы в репозитории kit:
[engines/own-api](https://github.com/LoomaFloat/looma-kit/tree/main/engines/own-api) (свой
API, двоичное тело, ответ по частям, своя единица, долгое вычисление),
[engines/llm](https://github.com/LoomaFloat/looma-kit/tree/main/engines/llm) (чат на весах
HuggingFace), [engines/echo](https://github.com/LoomaFloat/looma-kit/tree/main/engines/echo)
(весь договор в одном файле). Ниже каркас, в который оборачивают чужой код.

## Свой API с загрузкой модели и долгим заданием

Только стандартная библиотека: зависимости службы не спорят с зависимостями модели.
`load()` и `compute()` заменяются кодом автора.

```python
"""Служба движка: здоровье, быстрый вызов, долгое задание."""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

OPTIONS = json.loads(os.environ.get("LOOMA_ENGINE_OPTIONS") or "{}")
CACHE = os.environ.get("LOOMA_CACHE_DIR") or "/tmp"
STATE = {"ready": False, "model": None}
JOBS: dict = {}                     # номер задания площадки → {"progress", "result", "error"}
LOCK = threading.Lock()


def load():
    """Загрузить модель (код автора). Веса — в HF_HOME или CACHE."""
    return object()


def compute(model, params: dict, progress=lambda percent: None) -> bytes:
    """Само вычисление (код автора). Возвращает тело ответа."""
    progress(100)
    return json.dumps({"ok": True, "params": params}).encode()


def warm() -> None:
    try:
        STATE["model"] = load()
        STATE["ready"] = True
    except Exception:
        traceback.print_exc()           # причина в лог реплики (looma model logs)
        os._exit(1)                     # не висеть с закрытым здоровьем


def run_job(number: str, params: dict) -> None:
    def progress(percent: int) -> None:
        JOBS[number]["progress"] = int(percent)
    try:
        JOBS[number]["result"] = compute(STATE["model"], params, progress)
    except Exception as exc:
        JOBS[number]["error"] = str(exc)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass

    def _send(self, status: int, body: bytes, kind: str = "application/json", **headers):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), str(value))
        self.end_headers()
        self.wfile.write(body)

    def _params(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(size) if size else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:                                   # noqa: N802
        url = urlsplit(self.path)
        if url.path == "/health":
            return self._send(200 if STATE["ready"] else 503, b"{}")
        if url.path.startswith("/jobs/"):                       # опрос долгого задания
            job = JOBS.get(url.path.split("/")[2])
            if job is None:
                return self._send(404, b'{"error": "no such job"}')
            if job.get("error"):
                return self._send(500, json.dumps({"error": job["error"]}).encode())
            if job.get("result") is None:
                return self._send(202, b"{}", X_Looma_Progress=job["progress"],
                                  Retry_After=10)
            return self._send(200, job["result"], X_Looma_Units=1)
        self._send(404, b'{"error": "not found"}')

    def do_POST(self) -> None:                                  # noqa: N802
        if not STATE["ready"]:
            return self._send(503, b'{"error": "loading"}', Retry_After=30)
        url = urlsplit(self.path)
        params = {**OPTIONS, **{k: v[-1] for k, v in parse_qs(url.query).items()},
                  **self._params()}
        if url.path == "/compute":                              # быстрый вызов
            return self._send(200, compute(STATE["model"], params), X_Looma_Units=1)
        if url.path == "/long":                                 # долгое вычисление
            number = self.headers.get("X-Looma-Job") or os.urandom(6).hex()
            with LOCK:                                          # повтор не начинает заново
                if number not in JOBS:
                    JOBS[number] = {"progress": 0, "result": None, "error": None}
                    threading.Thread(target=run_job, args=(number, params),
                                     daemon=True).start()
            return self._send(202, b"{}", Location=f"/jobs/{number}")
        self._send(404, b'{"error": "not found"}')

    def do_DELETE(self) -> None:                                # noqa: N802
        JOBS.pop(urlsplit(self.path).path.split("/")[-1], None)  # отмена задания
        self._send(200, b"{}")


def main() -> None:
    port = int(os.environ.get("LOOMA_SERVE_PORT") or 8000)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)   # порт сразу
    threading.Thread(target=warm, daemon=True).start()          # модель рядом
    print(f"слушаю 127.0.0.1:{port}", file=sys.stderr, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
```

Что в нём важно для площадки:

- порт открыт сразу, модель грузится рядом; `/health` отвечает 503, пока не готово;
- ошибка загрузки: трассировка в лог и выход, а не вечное 503;
- долгое задание идемпотентно по `X-Looma-Job`: повтор площадки не начинает вторую
  работу;
- 202 с `X-Looma-Progress` и `Retry-After`, итог 200 с телом и `X-Looma-Units`;
- `DELETE` снимает задание.

Если вычисление не потокобезопасно (одна модель на карте), ставь задания в очередь
внутри службы: площадка шлёт одной реплике до четырёх заданий сразу.

## Чат

Возьми [engines/llm](https://github.com/LoomaFloat/looma-kit/tree/main/engines/llm) и
поменяй загрузку и генерацию. Обязательно: `usage` в ответе и в последнем событии потока,
`model` равно `LOOMA_MODEL_ID`, поток кончается `data: [DONE]`.

## FastAPI вместо стандартной библиотеки

Можно: `fastapi` и `uvicorn` в `requirements.txt`, запуск
`uvicorn.run(app, host="127.0.0.1", port=int(os.environ["LOOMA_SERVE_PORT"]))`, модель
грузится в фоновом потоке со старта. Договор тот же.
