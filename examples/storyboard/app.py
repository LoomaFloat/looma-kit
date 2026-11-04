"""Раскадровка: ролик со звуком по идее в одну строку. Авторский сервис на Looma Float.

    GET    /                              страница (static/)
    GET    /api/config                    форматы, длительности, модели
    GET    /api/projects                  мои проекты
    POST   /api/projects                  {"idea", "format", "seconds", "quality", "auto"}: новый проект
    GET    /api/projects/<id>             проект целиком
    DELETE /api/projects/<id>             удалить с кадрами и роликами
    POST   /api/projects/<id>/script      {"note"?}: написать сценарий заново
    PUT    /api/projects/<id>/script      {"title"?, "frame_prompt"?, "video_prompt"?, "end_prompt"?}
    POST   /api/projects/<id>/frames      {"kind": "start" | "end"}: нарисовать варианты
    PUT    /api/projects/<id>/frames      {"start"?, "end"?}: выбрать кадры
    POST   /api/projects/<id>/takes       снять дубль
    GET    /media/<id>/<файл>             кадр или ролик
    POST   /mcp/make_video, /mcp/video_status   инструменты MCP (раздел channels в looma.yaml)

Всё, что считается дольше секунды, идёт в фоне: ответ приходит сразу, а
страница опрашивает проект. Клиента каталога передаёт заголовок
X-Looma-Client, его ставит шлюз платформы. У себя (`looma dev`) клиента нет,
и все запросы идут от одного посетителя.
"""

import hashlib
import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from loomafloat import client_of

import pipeline
from pipeline import Problem

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
APP_URL = os.environ.get("LOOMA_APP_URL", "").rstrip("/")


def _assets():
    """Файлы страницы лежат в памяти, с ETag: повторный заход их не качает."""
    out = {}
    for path in STATIC.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/javascript", "image/svg+xml"):
                kind += "; charset=utf-8"
            out[path.relative_to(STATIC).as_posix()] = (
                data, kind, '"' + hashlib.sha1(data).hexdigest()[:16] + '"')
    return out


ASSETS = _assets()
ROUTE = re.compile(r"^/api/projects/([0-9a-f]{16})(?:/(script|frames|takes))?$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------------- ответы
    def _send(self, status, body, kind="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message):
        self._send(status, {"error": message})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise Problem(413, "Слишком большой запрос")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise Problem(400, "Тело запроса должно быть JSON") from None
        if not isinstance(body, dict):
            raise Problem(400, "Тело запроса должно быть JSON-объектом")
        return body

    def _client(self):
        return client_of(self.headers) or ""

    def _asset(self, name):
        data, kind, etag = ASSETS[name]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, data, kind, {"ETag": etag, "Cache-Control": "no-cache"})

    def _media(self, data, kind, download=None):
        """Кадр или ролик с поддержкой Range: без неё Safari не играет ролик."""
        extra = {"Cache-Control": "private, max-age=31536000, immutable",
                 "Accept-Ranges": "bytes"}
        if download:
            extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(download)}"
        found = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if not found or not data:
            return self._send(200, data, kind, extra)
        start = int(found.group(1) or 0)
        end = min(int(found.group(2)) if found.group(2) else len(data) - 1, len(data) - 1)
        if start > end:
            return self._send(416, b"", kind, {"Content-Range": f"bytes */{len(data)}"})
        extra["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        return self._send(206, data[start:end + 1], kind, extra)

    # -------------------------------------------------------- маршруты
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        try:
            if path == "/health":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            if path == "/api/config":
                return self._send(200, config())
            if path == "/api/projects":
                return self._send(200, {"data": pipeline.projects(self._client())})
            found = ROUTE.match(path)
            if found and not found.group(2):
                project = pipeline.fresh(self._client(), found.group(1))
                return self._send(200, pipeline.public(project))
            found = re.match(r"^/media/([0-9a-f]{16})/([\w.-]+)$", path)
            if found:
                return self._file(found.group(1), found.group(2), "download=1" in url.query)
            if path.startswith("/static/") and path[len("/static/"):] in ASSETS:
                return self._asset(path[len("/static/"):])
            if path.startswith("/api/") or path.startswith("/media/"):
                return self._error(404, "Нет такого адреса")
            return self._asset("index.html")    # маршруты страницы (#/p/…) разбирает она сама
        except Problem as exc:
            return self._error(exc.status, exc.message)

    def _file(self, pid, name, download):
        client = self._client()
        key = pipeline.media_key(client, pid, name)
        if not key:
            return self._error(404, "Нет такого файла")
        try:
            data = pipeline.store.get(key)
        except KeyError:
            return self._error(404, "Нет такого файла")
        kind = "video/mp4" if name.endswith(".mp4") else "image/png"
        filename = None
        if download:
            try:
                title = pipeline.load(pipeline.folder_of(client), pid)["script"]["title"]
            except Problem:
                title = ""
            filename = f"{title or 'Раскадровка'} {name}".replace("/", " ")
        return self._media(data, kind, filename)

    def do_POST(self):
        path, client = urlparse(self.path).path, self._client()
        try:
            body = self._body()
            if path == "/api/projects":
                project = pipeline.create(client, body.get("idea"), body.get("format", "16:9"),
                                          _int(body.get("seconds", 5)),
                                          body.get("quality", "fast"), body.get("auto", False))
                return self._send(201, pipeline.public(project))
            if path == "/mcp/make_video":
                return self._send(200, mcp_make(client, body))
            if path == "/mcp/video_status":
                return self._send(200, mcp_status(client, body))
            found = ROUTE.match(path)
            if not found or not found.group(2):
                return self._error(404, "Нет такого адреса")
            pid, part = found.groups()
            if part == "script":
                project = pipeline.write_script(client, pid, body.get("note", ""))
            elif part == "frames":
                project = pipeline.draw(client, pid, body.get("kind", "start"))
            else:
                project = pipeline.shoot(client, pid)
            return self._send(202, pipeline.public(project))
        except Problem as exc:
            return self._error(exc.status, exc.message)

    def do_PUT(self):
        found = ROUTE.match(urlparse(self.path).path)
        if not found or found.group(2) not in ("script", "frames"):
            return self._error(404, "Нет такого адреса")
        pid, part = found.groups()
        try:
            body = self._body()
            if part == "script":
                project = pipeline.edit_script(self._client(), pid, body)
            else:
                project = pipeline.choose(self._client(), pid, body.get("start"), body.get("end"))
            return self._send(200, pipeline.public(project))
        except Problem as exc:
            return self._error(exc.status, exc.message)

    def do_DELETE(self):
        found = ROUTE.match(urlparse(self.path).path)
        if not found or found.group(2):
            return self._error(404, "Нет такого адреса")
        try:
            pipeline.remove(self._client(), found.group(1))
            return self._send(200, {"deleted": found.group(1)})
        except Problem as exc:
            return self._error(exc.status, exc.message)

    def log_message(self, fmt, *args):
        if "/health" not in self.path:
            print(f"{self.command} {self.path} → {args[1] if len(args) > 1 else ''}", flush=True)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def config():
    return {"formats": list(pipeline.FORMATS), "seconds": list(pipeline.SECONDS),
            "qualities": list(pipeline.QUALITIES),
            "models": {"script": pipeline.SCRIPT_MODEL, "frames": pipeline.FRAME_MODEL,
                       "video": pipeline.VIDEO_MODEL},
            "environment": os.environ.get("LOOMA_ENVIRONMENT", "local")}


# ------------------------------------------------------------ MCP
def _page(pid):
    return f"{APP_URL}/#/p/{pid}" if APP_URL else f"#/p/{pid}"


def mcp_make(client, body):
    """Инструмент make_video: весь конвейер без остановок, ответ сразу."""
    project = pipeline.create(client, body.get("idea"), body.get("format", "16:9"),
                              _int(body.get("seconds", 5)), "fast", auto=True)
    return {"id": project["id"], "status": "working", "page": _page(project["id"]),
            "message": "Ролик в работе: сценарий, кадр, затем съёмка, всего около 7–10 минут. "
                       "Статус: инструмент video_status с этим id."}


def mcp_status(client, body):
    pid = str(body.get("id") or "")
    current = pipeline.fresh(client, pid)
    card, project = pipeline.summary(current), pipeline.public(current)
    last = project["takes"][-1] if project["takes"] else None
    out = {"id": pid, "title": card["title"], "state": card["state"], "stage": card["stage"],
           "page": _page(pid)}
    if last and last["status"] == "working":
        out["progress"] = last["progress"]
    if card["state"] == "ready":
        out["message"] = "Ролик готов: смотреть и скачать по ссылке page."
    elif card["state"] == "failed":
        errors = [project["script"]["error"], project["frames"]["error"],
                  last["error"] if last else ""]
        out["error"] = next((e for e in errors if e), "не получилось")
    return out


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"раскадровка: {pipeline.SCRIPT_MODEL} → {pipeline.FRAME_MODEL} → "
          f"{pipeline.VIDEO_MODEL}; проекты: {pipeline.store.where()}; "
          f"слушаю 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
