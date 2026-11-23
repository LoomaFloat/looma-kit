"""Клиент API Looma Float: модели для кода сервиса, сервисы для CLI и MCP.

    from loomafloat import Looma

    looma = Looma()        # ключ и адрес — из окружения или `looma login`
    text = looma.chat("Qwen3.8-Flash-Next-NVFP4", "Привет!")
    png = looma.image("Qwen-Image-2.1", "рыжий кот в скафандре")[0]
    vectors = looma.embed("bge-m3", ["первый кусок", "второй кусок"], input_type="document")
    best = looma.rerank("bge-reranker-v2-m3", "вопрос", ["кусок 1", "кусок 2"], top_n=1)
    mp4 = looma.video("MiniMax-H3", "A red fox in a snowy forest", quality="fast").content()
    answer = looma.run("anna/solver", "/tasks", body=b"...")          # модель со своим API
    job = looma.run("anna/solver", "/render", body={"frames": 240}, wait=False)
    result = job.wait().content()                                      # долгое вычисление

Внутри процесса сервиса ключ — `LOOMA_APP_TOKEN`, адрес — `LOOMA_API_URL`:
платформа кладёт их сама, `Looma()` находит их без настроек.

Клиент маркетплейса — заголовок `X-Looma-Client` запроса к сервису
(`client_of(headers)`). Вызовы для него — `looma.for_client(ref).chat(…)`:
они записываются на клиента; событие для учёта — `looma.charge("answer", ref)`.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import struct
import time
import urllib.parse
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Union

from loomafloat import config
from loomafloat.http import LoomaError, get_json, open_stream, request, send_json

ImageInput = Union[str, Path, bytes]
#: Сколько ждать изображение или чат: спящую модель будит первый запрос — минуты.
CALL_TIMEOUT_S = 900.0


def _data_url(item: ImageInput) -> str:
    """Изображение для правки: ссылка http(s) как есть, файл или байты — data-URL."""
    if isinstance(item, str) and item.startswith(("http://", "https://", "data:")):
        return item
    if isinstance(item, (str, Path)):
        path = Path(item)
        data = path.read_bytes()
        kind = mimetypes.guess_type(path.name)[0] or "image/png"
    else:
        data, kind = bytes(item), "image/png"
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


def _q(name: str) -> str:
    return urllib.parse.quote(str(name), safe="")


class Video:
    """Задание на видео: статус, ожидание, файл."""

    def __init__(self, looma: "Looma", doc: dict) -> None:
        self.looma = looma
        self.doc = doc

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def status(self) -> str:
        return self.doc.get("status", "")

    def refresh(self) -> "Video":
        self.doc = get_json(self.looma.url(f"/v1/videos/{self.id}"), key=self.looma.key)
        return self

    def wait(self, *, poll_s: float = 15.0, timeout_s: float = 3 * 3600.0) -> "Video":
        deadline = time.monotonic() + timeout_s
        if self.status not in ("completed", "failed", "cancelled"):
            self.refresh()              # могло досчитаться, пока вызывающий был занят
        while self.status not in ("completed", "failed", "cancelled"):
            if time.monotonic() > deadline:
                raise LoomaError(0, f"видео {self.id} не готово за {timeout_s:.0f} с")
            time.sleep(poll_s)
            self.refresh()
        if self.status != "completed":
            error = self.doc.get("error")
            why = error.get("message") if isinstance(error, dict) else error
            raise LoomaError(0, f"видео {self.id}: {self.status}" + (f" — {why}" if why else ""))
        return self

    def content(self) -> bytes:
        """mp4 — когда готово (ждёт, если ещё нет)."""
        if self.status != "completed":
            self.wait()
        return self.looma.download(f"/v1/videos/{self.id}/content")

    def save(self, path: Union[str, Path]) -> Path:
        target = Path(path)
        target.write_bytes(self.content())
        return target


def client_of(headers) -> Optional[str]:
    """Клиент маркетплейса из заголовков запроса к сервису (`X-Looma-Client`,
    его ставит шлюз платформы); None — пришёл не клиент маркетплейса."""
    if headers is None:
        return None
    for name in ("X-Looma-Client", "x-looma-client"):
        value = headers.get(name) if hasattr(headers, "get") else None
        if value:
            return str(value).strip()
    if isinstance(headers, dict):
        found = {k.lower(): v for k, v in headers.items()}.get("x-looma-client")
        return str(found).strip() if found else None
    return None


class Answer:
    """Ответ модели со своим API: статус, заголовки и тело как их отдала служба."""

    def __init__(self, status: int, headers: dict, content: bytes) -> None:
        self.status = status
        self.headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content or b"null")

    def __repr__(self) -> str:
        return f"<Answer {self.status}, {len(self.content)} байт>"


class Job:
    """Задание площадки (`/v1/jobs`): долгий вызов модели со своим API и
    другие задания. Статусы: queued, running, succeeded, failed, cancelled."""

    DONE = ("succeeded", "failed", "cancelled")

    def __init__(self, looma: "Looma", doc: dict) -> None:
        self.looma = looma
        self.doc = doc

    @property
    def id(self) -> str:
        return self.doc["id"]

    @property
    def status(self) -> str:
        return self.doc.get("status", "")

    @property
    def progress(self) -> Optional[int]:
        return self.doc.get("progress")

    def refresh(self) -> "Job":
        self.doc = get_json(self.looma.url(f"/v1/jobs/{_q(self.id)}"), key=self.looma.key)
        return self

    def wait(self, *, poll_s: float = 10.0, timeout_s: float = 24 * 3600.0) -> "Job":
        """Дождаться конца. Отказ службы (статус 400 и выше) — не ошибка
        задания: его тело тоже результат, смотрите `result["status"]`."""
        deadline = time.monotonic() + timeout_s
        if self.status not in self.DONE:
            self.refresh()
        while self.status not in self.DONE:
            if time.monotonic() > deadline:
                raise LoomaError(0, f"задание {self.id} не готово за {timeout_s:.0f} с")
            time.sleep(poll_s)
            self.refresh()
        if self.status != "succeeded" and not (self.doc.get("result") or {}).get("url"):
            error = self.doc.get("error")
            why = error.get("message") if isinstance(error, dict) else error
            raise LoomaError(0, f"задание {self.id}: {self.status}" + (f" — {why}" if why else ""))
        return self

    def content(self) -> bytes:
        """Тело итогового ответа службы (ждёт, если задание ещё идёт)."""
        if self.status not in self.DONE:
            self.wait()
        return self.looma.download(f"/v1/jobs/{_q(self.id)}/content")

    def save(self, path: Union[str, Path]) -> Path:
        target = Path(path)
        target.write_bytes(self.content())
        return target

    def cancel(self) -> "Job":
        _s, _h, body = request("DELETE", self.looma.url(f"/v1/jobs/{_q(self.id)}"),
                               key=self.looma.key)
        if body:
            self.doc = json.loads(body)
        return self


class Looma:
    def __init__(self, api_url: Optional[str] = None, key: Optional[str] = None,
                 client: Optional[str] = None) -> None:
        self.base = config.api_url(api_url)
        self.key = config.api_key(key)
        #: Клиент маркетплейса, для которого делаются вызовы моделей.
        self.client = client
        self.apps = Apps(self)
        self.entries = Entries(self)
        self.engines = Engines(self)
        self.nodes = Nodes(self)
        self.comfy = Comfy(self)

    def for_client(self, client: Optional[str]) -> "Looma":
        """Те же ключ и адрес, вызовы моделей — для клиента `client`."""
        return Looma(api_url=self.base, key=self.key, client=client)

    def _headers(self) -> dict:
        return {"X-Looma-Client": self.client} if self.client else {}

    def charge(self, event: str, client: Optional[str] = None, *, quantity: float = 1,
               id: Optional[str] = None) -> dict:
        """Отметить событие сервиса (из раздела events looma.yaml) для
        клиента маркетплейса. `id` — чтобы повтор не записался дважды."""
        client = client or self.client
        if not client:
            raise LoomaError(400, "charge: какой клиент? — client_of(заголовки запроса)")
        payload = {"client": client, "event": event, "quantity": quantity}
        if id:
            payload["id"] = id
        return send_json("POST", self.url("/v1/charge"), payload, key=self.key)

    def url(self, path: str) -> str:
        return self.base + path

    def download(self, path: str) -> bytes:
        """Файл результата: API отвечает ссылкой на хранилище (подписанной,
        на час) — за ней идём без ключа: он нужен только API."""
        status, head, body = request("GET", self.url(path), key=self.key, follow=False,
                                     timeout=CALL_TIMEOUT_S)
        location = {k.lower(): v for k, v in head.items()}.get("location")
        if 300 <= status < 400 and location:
            _s, _h, body = request("GET", location, timeout=CALL_TIMEOUT_S)
        return body

    # ------------------------------------------------------------ модели
    def models(self) -> List[dict]:
        return get_json(self.url("/v1/models"), key=self.key)["data"]

    @staticmethod
    def _chat_body(model: str, messages, params: dict) -> dict:
        body = {"model": model, **params}
        if messages is not None:
            if isinstance(messages, str):
                messages = [{"role": "user", "content": messages}]
            body["messages"] = list(messages)
        return body

    def chat(self, model: str, messages: Union[str, Iterable[dict], None] = None,
             **params) -> str:
        """Ответ модели текстом. `messages` — строка (одно сообщение
        пользователя) или список в формате OpenAI; прочее — как в OpenAI
        (`temperature`, `max_tokens`, …).

        Настройку автора со своим входом зовут полями вместо сообщений:
        `looma.chat("anna/translate", input={"text": "…"})`."""
        return self.chat_raw(model, messages, **params)["choices"][0]["message"]["content"]

    def chat_stream(self, model: str, messages: Union[str, Iterable[dict], None] = None,
                    **params) -> Iterator[str]:
        """Ответ кусками по мере генерации — для чатов, где текст печатается
        на глазах."""
        body = json.dumps({**self._chat_body(model, messages, params), "stream": True}).encode()
        answer = open_stream("POST", self.url("/v1/chat/completions"), key=self.key, body=body,
                             headers={"Content-Type": "application/json", **self._headers()},
                             timeout=CALL_TIMEOUT_S)
        with answer:
            for raw in answer:
                line = raw.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    return
                try:
                    doc = json.loads(data)
                except ValueError:
                    continue
                if doc.get("error"):
                    error = doc["error"]
                    raise LoomaError(0, error.get("message") if isinstance(error, dict)
                                     else str(error))
                for choice in doc.get("choices") or []:
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece

    def chat_raw(self, model: str, messages: Union[str, Iterable[dict], None] = None,
                 **params) -> dict:
        return send_json("POST", self.url("/v1/chat/completions"),
                         self._chat_body(model, messages, params),
                         key=self.key, timeout=CALL_TIMEOUT_S, headers=self._headers())

    def image(self, model: str, prompt: str = "", *,
              images: Optional[Iterable[ImageInput]] = None,
              n: int = 1, size: Optional[str] = None, **params) -> List[bytes]:
        """Изображения PNG байтами. С `images` (файлы, байты, ссылки) — правка по
        ним. Поля движка (`seed`, `num_inference_steps`, …) — как есть. Настройке
        автора со своим входом вместо промпта шлют `input={…}`."""
        body = {"model": model, "prompt": prompt, "n": n, "response_format": "b64_json",
                **params}
        if size:
            body["size"] = size
        path = "/v1/images/generations"
        if images:
            body["images"] = [_data_url(item) for item in images]
            path = "/v1/images/edits"
        answer = send_json("POST", self.url(path), body, key=self.key, timeout=CALL_TIMEOUT_S,
                           headers=self._headers())
        return [base64.b64decode(item["b64_json"]) for item in answer["data"]]

    def embed(self, model: str, texts: Union[str, Iterable[str]], *,
              input_type: Optional[str] = None,
              dimensions: Optional[int] = None) -> List[List[float]]:
        """Векторы текстов по порядку; строка даёт список из одного вектора.
        `input_type`: `query` для поиска, `document` для индексации. Моделям e5 и
        Qwen3 нужны свои префиксы, их подставляет сервер. `dimensions` укорачивает
        вектор у моделей с Matryoshka (вектор перенормирован)."""
        body = {"model": model, "input": [texts] if isinstance(texts, str) else list(texts),
                # base64 вдвое легче чисел текстом; читаем так же, как SDK OpenAI.
                "encoding_format": "base64"}
        if input_type:
            body["input_type"] = input_type
        if dimensions:
            body["dimensions"] = dimensions
        answer = send_json("POST", self.url("/v1/embeddings"), body, key=self.key,
                           timeout=CALL_TIMEOUT_S, headers=self._headers())
        out = []
        for item in sorted(answer["data"], key=lambda d: d["index"]):
            raw = base64.b64decode(item["embedding"])
            out.append(list(struct.unpack(f"<{len(raw) // 4}f", raw)))
        return out

    def rerank(self, model: str, query: str, documents: Iterable[str], *,
               top_n: Optional[int] = None) -> List[dict]:
        """Документы по убыванию оценки: `{"index", "relevance_score"}`, где
        `index` указывает в `documents`. `top_n`: сколько лучших вернуть."""
        body = {"model": model, "query": query, "documents": list(documents),
                "return_documents": False}
        if top_n:
            body["top_n"] = top_n
        answer = send_json("POST", self.url("/v1/rerank"), body, key=self.key,
                           timeout=CALL_TIMEOUT_S, headers=self._headers())
        return answer["results"]

    def video(self, model: str, prompt: str = "", *, seconds: float = 5, wait: bool = False,
              **params) -> Video:
        """Задание на видео; `wait=True` — дождаться. Поля — как у
        `POST /v1/videos` (`quality`, `size`, `seed`, `conditions`, …). Настройке
        автора со своим входом шлют `input={…}`."""
        doc = send_json("POST", self.url("/v1/videos"),
                        {"model": model, "prompt": prompt, "seconds": seconds, **params},
                        key=self.key, headers=self._headers())
        job = Video(self, doc)
        return job.wait() if wait else job

    def run(self, model: str, path: str = "/", *, method: str = "POST", body=None,
            headers: Optional[dict] = None, wait: bool = True) -> Union[Answer, Job]:
        """Вызов модели со своим API (движок с `kinds: [api]`): запрос уходит
        службе как есть, ответ возвращается как есть. `model` — `автор/модель`,
        `path` — путь у службы со строкой запроса. `body`: байты, строка или
        объект (уйдёт JSON).

        `wait=False` — долгое вычисление: площадка принимает запрос заданием
        (`Prefer: respond-async`) и доводит до конца сама; ответ — `Job`."""
        author, _, name = str(model).partition("/")
        if not author or not name:
            raise LoomaError(400, "model: автор/модель, например anna/solver")
        path = "/" + str(path or "/").lstrip("/")
        outgoing = {**self._headers(), **(headers or {})}
        data: Optional[bytes]
        if body is None:
            data = None
        elif isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body, ensure_ascii=False).encode()
            outgoing.setdefault("Content-Type", "application/json")
        if not wait:
            outgoing["Prefer"] = "respond-async"
        url = self.url(f"/v1/run/{_q(author)}/{_q(name)}{path}")
        status, head, content = request(method.upper(), url, key=self.key, body=data,
                                        headers=outgoing, timeout=CALL_TIMEOUT_S)
        if not wait and status == 202:
            return Job(self, json.loads(content))
        return Answer(status, head, content)

    def job(self, job_id: str) -> Job:
        """Задание по номеру: `run_…` и другие."""
        return Job(self, {"id": job_id}).refresh()

    def income(self, period: str = "month") -> dict:
        """Доход автора за период (`week`, `month`, `all`): сервисы, свои
        модели, движки и узлы. Выплат пока нет: это учёт."""
        return get_json(self.url(f"/v1/income?period={_q(period)}"), key=self.key)

    def whoami(self) -> dict:
        return get_json(self.url("/api/me"), key=self.key)


class Apps:
    """Сервисы автора — то, на чём стоят CLI `looma` и MCP."""

    def __init__(self, looma: Looma) -> None:
        self.looma = looma

    def _url(self, *parts: str) -> str:
        return self.looma.url("/v1/apps" + "".join(f"/{_q(p)}" for p in parts))

    def _get(self, *parts: str, query: str = ""):
        return get_json(self._url(*parts) + (f"?{query}" if query else ""), key=self.looma.key)

    def _post(self, *parts: str, payload=None):
        return send_json("POST", self._url(*parts), payload, key=self.looma.key)

    def list(self) -> List[dict]:
        return self._get()["data"]

    def get(self, app: str) -> dict:
        return self._get(app)

    def find(self, app: str) -> Optional[dict]:
        try:
            return self.get(app)
        except LoomaError as exc:
            if exc.status == 404:
                return None
            raise

    def create(self, name: str) -> dict:
        return send_json("POST", self._url(), {"name": name}, key=self.looma.key)

    def delete(self, app: str) -> dict:
        _s, _h, body = request("DELETE", self._url(app), key=self.looma.key)
        return json.loads(body) if body else {}

    # релизы
    def release(self, app: str, archive: bytes) -> dict:
        _s, _h, body = request("POST", self._url(app, "releases"), key=self.looma.key,
                               body=archive, timeout=600.0,
                               headers={"Content-Type": "application/gzip"})
        return json.loads(body)

    def releases(self, app: str) -> List[dict]:
        return self._get(app, "releases")["data"]

    def release_info(self, app: str, number: int) -> dict:
        return self._get(app, "releases", str(number))

    def release_log(self, app: str, number: int) -> str:
        _s, _h, body = request("GET", self._url(app, "releases", str(number), "log"),
                               key=self.looma.key)
        return body.decode(errors="replace")

    def wait_release(self, app: str, number: int, *, poll_s: Optional[float] = None,
                     timeout_s: float = 1800.0, on_poll=None) -> dict:
        if poll_s is None:
            poll_s = float(os.environ.get("LOOMA_POLL_S", "3") or 3)
        deadline = time.monotonic() + timeout_s
        while True:
            info = self.release_info(app, number)
            if info["status"] != "building":
                return info
            if time.monotonic() > deadline:
                raise LoomaError(0, f"релиз {number} не собрался за {timeout_s:.0f} с")
            if on_poll:
                on_poll(info)
            time.sleep(poll_s)

    # окружения
    def environments(self, app: str) -> List[dict]:
        return self._get(app, "environments")["data"]

    def promote(self, app: str, release: Optional[int] = None) -> dict:
        return self._post(app, "promote", payload={"release": release} if release else {})

    def rollback(self, app: str, environment: str = "prod") -> dict:
        return self._post(app, "rollback", payload={"environment": environment})

    def deploy_to(self, app: str, environment: str, release: Optional[int]) -> dict:
        return self._post(app, "environments", environment, payload={"release": release})

    def stop(self, app: str, environment: Optional[str] = None) -> dict:
        return self._post(app, "stop", payload={"environment": environment} if environment else {})

    # процессы
    def processes(self, app: str, environment: Optional[str] = None) -> List[dict]:
        query = f"environment={_q(environment)}" if environment else ""
        return self._get(app, "processes", query=query)["data"]

    def logs(self, app: str, process: str, *, environment: str = "prod", instance: int = 0,
             tail: int = 200) -> str:
        url = (self._url(app, "processes", process, "logs")
               + f"?environment={_q(environment)}&instance={instance}&tail={tail}")
        _s, _h, body = request("GET", url, key=self.looma.key)
        return body.decode(errors="replace")

    def restart(self, app: str, process: str, environment: str = "prod") -> List[dict]:
        url = self._url(app, "processes", process, "restart") + f"?environment={_q(environment)}"
        return send_json("POST", url, {}, key=self.looma.key)["data"]

    # секреты и данные
    def secrets(self, app: str) -> List[dict]:
        return self._get(app, "secrets")["data"]

    def set_secrets(self, app: str, values: dict) -> List[dict]:
        return send_json("PUT", self._url(app, "secrets"), values, key=self.looma.key)["data"]

    def data(self, app: str) -> List[dict]:
        return self._get(app, "data")["data"]

    def rotate_token(self, app: str) -> dict:
        return self._post(app, "token", "rotate")

    def check_manifest(self, text: str) -> dict:
        """Проверить `looma.yaml` без релиза: ошибки и предупреждения о моделях."""
        return send_json("POST", self.looma.url("/v1/manifests/check"), {"manifest": text},
                         key=self.looma.key)

    def stats(self, app: str, days: int = 30) -> dict:
        """Статистика автора: запросы по каналам, клиенты, события, модели, себестоимость."""
        return self._get(app, "stats", query=f"days={int(days)}")

    # маркетплейс
    def publish(self, app: str) -> dict:
        """Карточка из релиза на prod — на модерацию."""
        return self._post(app, "listing")

    def listing(self, app: str) -> dict:
        return self._get(app, "listing")

    def unpublish(self, app: str) -> dict:
        _s, _h, body = request("DELETE", self._url(app, "listing"), key=self.looma.key)
        return json.loads(body) if body else {}

    # looma dev
    def dev(self, app: str, manifest: str) -> dict:
        """Dev-токен для своей машины: модели и манифест — из присланного looma.yaml."""
        return self._post(app, "dev", payload={"manifest": manifest})

    def dev_quota(self, app: str) -> dict:
        return self._get(app, "dev")

    def dev_revoke(self, app: str) -> dict:
        _s, _h, body = request("DELETE", self._url(app, "dev"), key=self.looma.key)
        return json.loads(body) if body else {}


class Entries:
    """Свои модели автора в каталоге — то, на чём стоят `looma model` и MCP.

    Запись бывает трёх видов: свои веса на движке площадки (`repo`), модель
    каталога с адаптером LoRA (`base` и `adapter`) и настройка поверх модели
    каталога (`base` и `preset`: системный промпт, параметры, свой вход)."""

    def __init__(self, looma: Looma) -> None:
        self.looma = looma

    def _url(self, *parts) -> str:
        return self.looma.url("/api/services" + "".join(f"/{_q(str(p))}" for p in parts))

    def _send(self, method: str, url: str, payload=None) -> dict:
        return send_json(method, url, payload, key=self.looma.key)

    def mine(self) -> dict:
        """`{"services": […], "handle": имя автора}`."""
        return get_json(self._url(), key=self.looma.key)

    def list(self) -> List[dict]:
        return self.mine()["services"]

    def find(self, name: str) -> Optional[dict]:
        """Запись по полному имени `автор/модель`, по имени без автора или по номеру."""
        doc = self.mine()
        wanted = {name, f"{doc.get('handle') or ''}/{name}"}
        return next((e for e in doc["services"]
                     if e["label"] in wanted or str(e["id"]) == str(name)), None)

    def create(self, payload: dict) -> dict:
        return self._send("POST", self._url(), payload)

    def update(self, entry_id, payload: dict) -> dict:
        """Цена (`price`, копейки), описание (`summary`), настройка (`preset`),
        пределы реплик (`min_replicas`, `max_replicas`)."""
        return self._send("PATCH", self._url(entry_id), payload)

    def publish(self, entry_id) -> dict:
        """Подать на публикацию: после проверки площадки запись зовут все."""
        return self._send("POST", self._url(entry_id, "publish"))

    def unpublish(self, entry_id) -> dict:
        return self._send("POST", self._url(entry_id, "unpublish"))

    def delete(self, entry_id) -> dict:
        _s, _h, body = request("DELETE", self._url(entry_id), key=self.looma.key)
        return json.loads(body) if body else {}

    def logs(self, entry_id, tail: int = 200) -> dict:
        """Хвост лога реплик своей модели: почему движок не встал или падает."""
        return get_json(self._url(entry_id, "logs") + f"?tail={int(tail)}", key=self.looma.key)

    def check(self, payload: dict) -> dict:
        """Проверить веса без запуска: формат, размер, встанет ли модель в сети."""
        return self._send("POST", self.looma.url("/api/models/check"), payload)

    def engines(self) -> dict:
        """Движки площадки и поля их рецептов."""
        return get_json(self.looma.url("/api/engines"), key=self.looma.key)

    def set_handle(self, handle: str) -> dict:
        """Имя автора: с него начинаются имена его моделей. Задаётся один раз."""
        return self._send("PATCH", self.looma.url("/api/me"), {"handle": handle})

    def adapters(self) -> List[dict]:
        """Адаптеры LoRA проекта: из обучения и загруженные."""
        return get_json(self.looma.url("/v1/adapters"), key=self.looma.key)["data"]


class Comfy:
    """Граф ComfyUI как модель каталога (движок площадки `comfyui`)."""

    def __init__(self, looma: Looma) -> None:
        self.looma = looma

    def inspect(self, graph: dict) -> dict:
        """Разбор графа без запуска: узлы ядра, узлы наборов площадки и узлы,
        которых нет нигде; выход, файлы весов, поля, похожие на входы,
        черновик описания модели (с `extensions`, если граф пользуется
        наборами)."""
        return send_json("POST", self.looma.url("/api/comfyui/inspect"), {"graph": graph},
                         key=self.looma.key)

    def extensions(self) -> list:
        """Наборы узлов, которые можно назвать в модели: одобренные расширения
        движка `looma/comfyui` (свои и открытые) с узлами, загрузчиками и
        папками весов."""
        return (get_json(self.looma.url("/api/comfyui/extensions"), key=self.looma.key)
                or {}).get("extensions") or []


class Engines:
    """Свои движки и расширения автора — то, на чём стоит `looma engine`.

    Движок публикуют папкой из рабочей среды, как сервис (`submit_archive`):
    папка с `looma-engine.yaml` упаковывается и уходит на ревью площадки. По
    желанию вместо папки называют открытый репозиторий на GitHub и коммит
    (`submit`). После одобрения на движке собирают модели (`looma model add
    --engine автор/движок`). На свои узлы без ревью — `try_archive`."""

    def __init__(self, looma: Looma) -> None:
        self.looma = looma

    def _url(self, *parts) -> str:
        return self.looma.url("/api/layers" + "".join(f"/{_q(str(p))}" for p in parts))

    def mine(self) -> dict:
        """`{"layers": […], "handle": имя автора}`."""
        return get_json(self._url(), key=self.looma.key)

    def list(self) -> List[dict]:
        return self.mine()["layers"]

    def submit(self, *, repo: str, commit: str, path: str = "", kind: str = "engine",
               fee: Optional[dict] = None, own: bool = False) -> dict:
        """Подать версию на ревью из репозитория на GitHub (необязательный путь;
        основной — `submit_archive`). То же содержимое второй раз — прежняя версия.
        `fee` — доля автора движка с чужих вызовов, копейки за 1M токенов
        (`price_in`, `price_out`); закрепляется за версией. `own=True` — версия
        без ревью для своих узлов: одобрена сразу, работает только у автора."""
        payload = {"repo": repo, "commit": commit, "kind": kind}
        if path:
            payload["path"] = path
        if fee:
            payload["fee"] = fee
        if own:
            payload["own"] = True
        return send_json("POST", self._url(), payload, key=self.looma.key)

    def submit_archive(self, archive: bytes, *, kind: str = "engine",
                       fee: Optional[dict] = None, origin: str = "") -> dict:
        """Подать версию на ревью папкой движка: архив (tar.gz, как его делает
        `ops.pack_engine`) прямо из рабочей среды, без GitHub. `origin` —
        необязательная ссылка на код (репозиторий, страница); площадка её не
        проверяет, только показывает рядом с версией."""
        payload = {"kind": kind, "archive": base64.b64encode(archive).decode()}
        if fee:
            payload["fee"] = fee
        if origin:
            payload["origin"] = origin
        return send_json("POST", self._url(), payload, key=self.looma.key, timeout=300.0)

    def code(self, ref: str, *, number: Optional[int] = None) -> dict:
        """Код одобренной версии движка автора: источник, команда, зависимости,
        файлы. Его читает любой, кто вошёл: например, владелец узла, решающий,
        какие движки принимать. Без номера — последняя одобренная версия."""
        author, _, name = ref.partition("/")
        query = f"?version={int(number)}" if number else ""
        return get_json(self._url(author, name, "code") + query, key=self.looma.key)

    def code_file(self, ref: str, path: str, *, number: Optional[int] = None) -> dict:
        """Один файл одобренной версии, текстом."""
        author, _, name = ref.partition("/")
        query = {"path": path, **({"version": str(int(number))} if number else {})}
        return get_json(self._url(author, name, "code", "file") + "?"
                        + urllib.parse.urlencode(query),
                        key=self.looma.key)

    def try_archive(self, archive: bytes, *, kind: str = "engine") -> dict:
        """Версия для своих узлов из архива папки движка (tar.gz): без
        GitHub и без ревью. Работает только на узлах автора."""
        payload = {"own": True, "kind": kind,
                   "archive": base64.b64encode(archive).decode()}
        return send_json("POST", self._url(), payload, key=self.looma.key, timeout=300.0)

    def share(self, ref: str, public: bool) -> dict:
        """Открыть слой другим авторам или закрыть. `ref` — `автор/слой`."""
        author, _, name = ref.partition("/")
        return send_json("PATCH", self._url(author, name), {"public": bool(public)},
                         key=self.looma.key)


class Nodes:
    """Свои узлы автора: машины с видеокартой, подключённые к площадке.

    Ключом для выкатки (`looma login`) узел подключают «только для себя»,
    переименовывают и отключают. Делиться узлом с сетью и выбирать, чей код он
    принимает, решает владелец в кабинете автора."""

    def __init__(self, looma: Looma) -> None:
        self.looma = looma

    def _url(self, *parts) -> str:
        return self.looma.url("/api/nodes" + "".join(f"/{_q(str(p))}" for p in parts))

    def mine(self) -> dict:
        """`{"nodes": […], "keys": […]}`: узлы с состоянием, работой, движками,
        которые на них встанут, и начисленным."""
        return get_json(self._url(), key=self.looma.key)

    def list(self) -> List[dict]:
        return self.mine()["nodes"]

    def find(self, name: str) -> Optional[dict]:
        """Узел по имени или подписи."""
        return next((n for n in self.list() if name in (n["node_id"], n.get("title"))), None)

    def connect(self, label: str = "") -> dict:
        """Ключ и команда подключения новой машины. Ключ показывается один
        раз: команду запускают на машине с видеокартой."""
        return send_json("POST", self._url("keys"), {"label": label}, key=self.looma.key)

    def rename(self, node_id: str, title: str) -> dict:
        return send_json("PATCH", self._url(node_id), {"title": title}, key=self.looma.key)

    def disconnect(self, key_id: str) -> dict:
        """Отозвать ключ узла: машина отключается, работа на ней снимается."""
        _s, _h, body = request("DELETE", self._url("keys", key_id), key=self.looma.key)
        return json.loads(body) if body else {}

    def forget(self, node_id: str) -> dict:
        """Убрать из списка узел, которого нет на связи."""
        _s, _h, body = request("DELETE", self._url(node_id), key=self.looma.key)
        return json.loads(body) if body else {}


__all__ = ["Answer", "Apps", "Engines", "Entries", "Job", "Looma", "Nodes", "Video",
           "client_of"]
