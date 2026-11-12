"""Движок языковой модели на transformers.

Настоящий движок, а не учебный: скачивает открытые веса с HuggingFace, грузит
модель на одну видеокарту и отвечает на чат. Тот же файл работает в обоих
видах движка: процессом после ревью (`engines/llm`) и образом в песочнице
(`engines/llm-box`).

Договор с площадкой:

- слушать порт из `LOOMA_SERVE_PORT`. Процесс слушает петлю, служба в образе
  все адреса: в образе это включает `LLM_BIND=0.0.0.0` из Dockerfile;
- `GET /health` отвечает 503, пока веса качаются и грузятся, и 200 с
  `{"status": "ok"}`, когда модель готова. Не загрузилась: движок пишет
  причину в лог и завершается, а не висит с закрытым здоровьем;
- `POST /v1/chat/completions` в формате OpenAI, целиком и потоком; в ответе
  `usage`, в потоке он едет последним событием перед `[DONE]`;
- репозиторий весов приходит в `LOOMA_MODEL`, имя модели в `LOOMA_MODEL_ID`,
  поля рецепта одним JSON в `LOOMA_ENGINE_OPTIONS`.

Куда качать веса, движок не решает: `HF_HOME` уже указывает на кэш, который
переживает перезапуск (общий кэш узла у процесса, свой каталог `/cache` у
службы в образе).

    LOOMA_MODEL=Qwen/Qwen2.5-0.5B-Instruct LOOMA_SERVE_PORT=8000 python -m llm_engine.server
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Загрузчик xet на части машин обрывает большие закачки; обычный HTTP надёжнее.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

MODEL_ID = os.environ.get("LOOMA_MODEL_ID", "llm")
REPO = os.environ.get("LOOMA_MODEL", "")
OPTIONS = json.loads(os.environ.get("LOOMA_ENGINE_OPTIONS") or "{}")
#: Предел длины ответа: запрос может просить меньше, но не больше.
MAX_TOKENS = max(1, min(8192, int(OPTIONS.get("max_tokens") or 512)))
#: Сколько раз начинать закачку весов заново, если сеть оборвалась.
DOWNLOAD_ATTEMPTS = 3

STATE = {"ready": False, "stage": "запуск", "device": ""}
ENGINE: dict = {}
#: Одна генерация за раз: карта у реплики одна, а модель не делит её между
#: запросами. Остальные запросы ждут своей очереди.
BUSY = threading.Lock()


def load() -> None:
    """Скачать веса и поднять модель. Долго: здоровье всё это время 503."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not REPO:
        raise RuntimeError("репозиторий весов не назван: модель не задала LOOMA_MODEL")
    if torch.cuda.is_available():
        device = "cuda"
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    wanted = str(OPTIONS.get("dtype") or "auto")
    dtype = getattr(torch, wanted, None) if wanted != "auto" else None
    if dtype is None:
        dtype = torch.float32 if device == "cpu" else torch.float16
    STATE.update(stage=f"скачиваю и загружаю {REPO}", device=device)
    print(f"llm: {REPO} на {device}, {dtype}", flush=True)
    started = time.time()
    # Точность весов в новых transformers называется `dtype`, в старых `torch_dtype`.
    import transformers

    recent = tuple(int(part) for part in transformers.__version__.split(".")[:2]) >= (4, 56)
    precision = {"dtype": dtype} if recent else {"torch_dtype": dtype}
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            tokenizer = AutoTokenizer.from_pretrained(REPO)
            model = AutoModelForCausalLM.from_pretrained(REPO, **precision)
            break
        except OSError as exc:              # сеть или диск: стоит попробовать ещё
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            print(f"llm: веса не скачались ({exc}); попытка {attempt + 1}", flush=True)
            time.sleep(5 * attempt)
    model.to(device).eval()
    ENGINE.update(torch=torch, tokenizer=tokenizer, model=model, device=device)
    STATE.update(ready=True, stage="готов")
    print(f"llm: модель готова за {time.time() - started:.0f} с", flush=True)


def text_of(content) -> str:
    """Текст сообщения: строка как есть, список частей — только текстовые."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content
                       if isinstance(part, dict) and part.get("type") in (None, "text"))
    return ""


def prompt_ids(messages: list):
    """Запрос в токенах модели: её собственным шаблоном чата, если он есть."""
    tokenizer = ENGINE["tokenizer"]
    clean = [{"role": str(m.get("role") or "user"), "content": text_of(m.get("content"))}
             for m in messages if isinstance(m, dict)]
    if getattr(tokenizer, "chat_template", None):
        encoded = tokenizer.apply_chat_template(clean, add_generation_prompt=True,
                                                return_tensors="pt", return_dict=True)
    else:
        plain = "\n".join(f"{m['role']}: {m['content']}" for m in clean) + "\nassistant:"
        encoded = tokenizer(plain, return_tensors="pt")
    return encoded.to(ENGINE["device"])


def limits(body: dict) -> dict:
    """Параметры генерации из запроса, в пределах, которые задала модель."""
    asked = body.get("max_completion_tokens") or body.get("max_tokens") or MAX_TOKENS
    temperature = body.get("temperature")
    temperature = 0.7 if temperature is None else max(0.0, min(2.0, float(temperature)))
    made = {"max_new_tokens": max(1, min(MAX_TOKENS, int(asked))),
            "do_sample": temperature > 0}
    if temperature > 0:
        top_p = body.get("top_p")
        made.update(temperature=temperature,
                    top_p=1.0 if top_p is None else max(0.01, min(1.0, float(top_p))))
    else:
        # Без случайности настройки выборки, записанные в самой модели, не
        # действуют; сняты явно, чтобы библиотека не предупреждала о них.
        made.update(temperature=None, top_p=None, top_k=None)
    return made


def generate(messages: list, params: dict, on_text=None) -> dict:
    """Ответ модели. `on_text` получает куски текста по мере генерации."""
    torch, tokenizer, model = ENGINE["torch"], ENGINE["tokenizer"], ENGINE["model"]
    with BUSY:
        inputs = prompt_ids(messages)
        asked = int(inputs["input_ids"].shape[-1])
        kwargs = {**params, "pad_token_id": tokenizer.eos_token_id}
        result: dict = {}

        def run(streamer=None) -> None:
            try:
                with torch.inference_mode():
                    result["ids"] = model.generate(**inputs, streamer=streamer, **kwargs)
            except Exception as exc:        # noqa: BLE001: причину отдаём клиенту
                result["error"] = f"{type(exc).__name__}: {exc}"
                if streamer is not None:
                    streamer.end()

        if on_text is None:
            run()
        else:
            from transformers import TextIteratorStreamer

            streamer = TextIteratorStreamer(tokenizer, skip_prompt=True,
                                            skip_special_tokens=True)
            worker = threading.Thread(target=run, args=(streamer,), daemon=True)
            worker.start()
            for piece in streamer:
                if piece:
                    on_text(piece)
            worker.join()
        if "error" in result:
            raise RuntimeError(result["error"])
        made = result["ids"][0][asked:]
        text = tokenizer.decode(made, skip_special_tokens=True)
        return {"text": text, "prompt_tokens": asked, "completion_tokens": int(made.shape[-1]),
                "finish": "length" if int(made.shape[-1]) >= params["max_new_tokens"] else "stop"}


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

    def _event(self, body: dict) -> None:
        self._chunk(f"data: {json.dumps(body, ensure_ascii=False)}\n\n")

    def do_GET(self) -> None:                     # noqa: N802
        if self.path != "/health":
            self._json(404, {"error": {"message": "нет такого адреса"}})
        elif not STATE["ready"]:
            self._json(503, {"status": "loading", "stage": STATE["stage"]})
        else:
            self._json(200, {"status": "ok", "model": MODEL_ID, "repo": REPO,
                             "device": STATE["device"]})

    def do_POST(self) -> None:                    # noqa: N802
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": {"message": "нет такого адреса"}})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            messages = body["messages"]
            params = limits(body)
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": {"message": "нужен JSON с полем messages"}})
            return
        if not STATE["ready"]:
            self._json(503, {"error": {"message": f"модель ещё грузится: {STATE['stage']}"}})
            return
        head = {"id": f"chatcmpl-{uuid.uuid4().hex}", "created": int(time.time()),
                "model": MODEL_ID}
        if not body.get("stream"):
            try:
                made = generate(messages, params)
            except RuntimeError as exc:
                self._json(500, {"error": {"message": str(exc)}})
                return
            self._json(200, {**head, "object": "chat.completion", "usage": usage_of(made),
                             "choices": [{"index": 0, "finish_reason": made["finish"],
                                          "message": {"role": "assistant",
                                                      "content": made["text"]}}]})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunk = {**head, "object": "chat.completion.chunk"}
        try:
            made = generate(messages, params, on_text=lambda piece: self._event(
                {**chunk, "choices": [{"index": 0, "delta": {"content": piece},
                                       "finish_reason": None}]}))
            self._event({**chunk, "usage": usage_of(made),
                         "choices": [{"index": 0, "delta": {}, "finish_reason": made["finish"]}]})
        except RuntimeError as exc:
            self._event({"error": {"message": str(exc)}})
        self._chunk("data: [DONE]\n\n")
        self._chunk("")


def usage_of(made: dict) -> dict:
    return {"prompt_tokens": made["prompt_tokens"], "completion_tokens": made["completion_tokens"],
            "total_tokens": made["prompt_tokens"] + made["completion_tokens"]}


def boot() -> None:
    try:
        load()
    except Exception as exc:                      # noqa: BLE001: причина идёт в лог задачи
        print(f"llm: модель не загрузилась: {type(exc).__name__}: {exc}", flush=True)
        os._exit(1)


def main() -> None:
    port = int(os.environ.get("LOOMA_SERVE_PORT") or os.environ.get("PORT") or 8000)
    host = os.environ.get("LLM_BIND") or "127.0.0.1"
    # Порт слушаем сразу, модель грузим рядом: площадка видит, что служба жива
    # и ещё грузится, а не гадает, почему порт закрыт.
    threading.Thread(target=boot, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
