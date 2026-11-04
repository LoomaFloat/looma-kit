"""Клиент API Looma Float: модели для кода сервиса, сервисы для CLI и MCP.

    from loomafloat import Looma

    looma = Looma()        # ключ и адрес — из окружения или `looma login`
    text = looma.chat("Qwen3.8-Flash-Next-NVFP4", "Привет!")
    png = looma.image("Qwen-Image-2.1", "рыжий кот в скафандре")[0]
    vectors = looma.embed("bge-m3", ["первый кусок", "второй кусок"], input_type="document")
    best = looma.rerank("bge-reranker-v2-m3", "вопрос", ["кусок 1", "кусок 2"], top_n=1)
    mp4 = looma.video("MiniMax-H3", "A red fox in a snowy forest", quality="fast").content()

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


class Looma:
    def __init__(self, api_url: Optional[str] = None, key: Optional[str] = None,
                 client: Optional[str] = None) -> None:
        self.base = config.api_url(api_url)
        self.key = config.api_key(key)
        #: Клиент маркетплейса, для которого делаются вызовы моделей.
        self.client = client
        self.apps = Apps(self)

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

    def chat(self, model: str, messages: Union[str, Iterable[dict]], **params) -> str:
        """Ответ модели текстом. `messages` — строка (одно сообщение
        пользователя) или список в формате OpenAI; прочее — как в OpenAI
        (`temperature`, `max_tokens`, …)."""
        return self.chat_raw(model, messages, **params)["choices"][0]["message"]["content"]

    def chat_stream(self, model: str, messages: Union[str, Iterable[dict]],
                    **params) -> Iterator[str]:
        """Ответ кусками по мере генерации — для чатов, где текст печатается
        на глазах."""
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        body = json.dumps({"model": model, "messages": list(messages), "stream": True,
                           **params}).encode()
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

    def chat_raw(self, model: str, messages: Union[str, Iterable[dict]], **params) -> dict:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        return send_json("POST", self.url("/v1/chat/completions"),
                         {"model": model, "messages": list(messages), **params},
                         key=self.key, timeout=CALL_TIMEOUT_S, headers=self._headers())

    def image(self, model: str, prompt: str, *, images: Optional[Iterable[ImageInput]] = None,
              n: int = 1, size: Optional[str] = None, **params) -> List[bytes]:
        """Изображения PNG байтами. С `images` (файлы, байты, ссылки) — правка по
        ним. Поля движка (`seed`, `num_inference_steps`, …) — как есть."""
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

    def video(self, model: str, prompt: str, *, seconds: float = 5, wait: bool = False,
              **params) -> Video:
        """Задание на видео; `wait=True` — дождаться. Поля — как у
        `POST /v1/videos` (`quality`, `size`, `seed`, `conditions`, …)."""
        doc = send_json("POST", self.url("/v1/videos"),
                        {"model": model, "prompt": prompt, "seconds": seconds, **params},
                        key=self.key, headers=self._headers())
        job = Video(self, doc)
        return job.wait() if wait else job

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


__all__ = ["Apps", "Looma", "Video", "client_of"]
