"""Подставной API Looma в памяти: те же пути и ответы, что у оркестратора.

Настоящий путь CLI проверяется на стенде; здесь — что CLI и SDK шлют и как
читают ответы, без сети и без репозитория оркестратора.
"""

from __future__ import annotations

import base64
import io
import json
import re
import struct
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

KEY = "looma_sk_test"
DEV_TOKEN = "looma_dev_test"
#: Что «сервер» отдаёт на `looma dev`, если проверка не задала своё.
DEV_MANIFEST = {"runtime": {"kind": "python", "python": "3.12"},
                "processes": {"web": {"kind": "web", "cmd": "python app.py", "port": 8080}}}
PNG = b"\x89PNG\r\n\x1a\nfake"
MP4 = b"\x00\x00\x00\x18ftypmp42fake"


class State:
    def __init__(self):
        self.apps = {}
        self.calls = []
        self.archives = {}
        self.fail_build = False
        self.lock = threading.Lock()
        self.dev_manifest = DEV_MANIFEST
        self.dev_dockerfile = "FROM python:3.12-slim\n"
        self.dev_texts = []
        self.dev_calls = 0
        #: Свои модели автора в каталоге и его имя.
        self.entries = []
        self.handle = ""
        self.adapters = [{"id": "adp-1", "object": "adapter", "name": "legal",
                          "base_model": "Qwen/Qwen3-0.6B", "models": ["chat-model:legal"]}]

    def app(self, name):
        for app in self.apps.values():
            if app["name"] == name or app["id"] == name:
                return app
        return None


def _public(app):
    return {k: app[k] for k in ("id", "object", "name", "slug", "project_id", "staging_release",
                                "prod_release", "created_at")} | {
        "urls": {"prod": f"https://{app['slug']}.apps.example.ru/",
                 "staging": f"https://{app['slug']}-staging.apps.example.ru/"},
        "tls": {"state": "ok"}}


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, doc=None, *, raw=None, kind="application/json", headers=None):
            body = raw if raw is not None else json.dumps(doc if doc is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message):
            self._send(status, {"error": {"message": message, "type": "invalid_request_error"}})

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _route(self, method):
            url = urlparse(self.path)
            path, query = url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = self._body()
            state.calls.append((method, path, self.headers.get("Authorization", ""), body))
            if path == "/files/video.mp4":            # «хранилище»: без ключа
                if self.headers.get("Authorization"):
                    return self._error(400, "хранилище не берёт ключ API")
                return self._send(200, raw=MP4, kind="video/mp4")
            if path == "/api/device/code":
                state.device_polls = 0
                return self._send(200, {"device_code": "dev-secret", "user_code": "ABCD-EFGH",
                                        "verification_uri": "https://console.example.ru/device",
                                        "verification_uri_complete":
                                            "https://console.example.ru/device?code=ABCD-EFGH",
                                        "expires_in": 600, "interval": 0.01})
            if path == "/api/device/token":
                if json.loads(body).get("device_code") != "dev-secret":
                    return self._send(400, {"error": {"message": "код истёк",
                                                      "type": "expired_token"}})
                state.device_polls += 1
                if state.device_polls == 1:
                    return self._send(503, {"error": {"message": "перезапуск"}})
                if state.device_polls < 3:
                    return self._send(400, {"error": {"message": "ждём подтверждения",
                                                      "type": "authorization_pending"}})
                return self._send(200, {"key": KEY, "project_id": 7, "email": "author@example.ru"})
            if (self.headers.get("Authorization") == f"Bearer {DEV_TOKEN}"
                    and path.startswith(("/v1/chat/", "/v1/images", "/v1/models"))):
                state.dev_calls += 1
            elif self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._error(401, "нужно войти")
            with state.lock:
                return self._dispatch(method, path, query, body)

        def _entries(self, method, path, body):
            """Свои модели автора: `/api/services` как у оркестратора."""
            doc = json.loads(body) if body else {}
            parts = [p for p in path.split("/") if p][2:]
            if not parts:
                if method == "GET":
                    return self._send(200, {"services": state.entries, "handle": state.handle})
                if not state.handle:
                    return self._error(409, "сначала задайте имя автора: записи зовутся "
                                            "`автор/запись`")
                light = doc.get("preset") is not None
                if light and (doc.get("min_replicas") or doc.get("repo")):
                    return self._error(400, "у настройки нет своих реплик")
                recipe = {} if light else {k: v for k, v in doc.items() if k not in (
                    "name", "summary", "base", "preset", "min_replicas", "max_replicas")}
                if doc.get("adapter"):
                    recipe = {"repo": "Qwen/Qwen3-0.6B", "adapter": "adp-1"}
                entry = {"id": len(state.entries) + 1, "label": f"{state.handle}/{doc['name']}",
                         "form": "light" if light else "heavy", "recipe": recipe,
                         "status": "ready" if light else "cold", "replicas": [],
                         "min_replicas": doc.get("min_replicas", 0),
                         "max_replicas": doc.get("max_replicas", 1), "price": {},
                         "summary": doc.get("summary", ""), "visibility": "private",
                         "review": "", "review_note": "", "note": "",
                         "passport": {"kind": "text", "form": "light" if light else "heavy"}}
                if light:
                    entry.update(base=doc["base"], preset=doc["preset"])
                state.entries.append(entry)
                return self._send(200, entry)
            entry = next((e for e in state.entries if str(e["id"]) == parts[0]), None)
            if entry is None:
                return self._error(404, f"сервиса {parts[0]} за вами не числится")
            if method == "DELETE":
                state.entries.remove(entry)
                return self._send(200, {**entry, "state": "deleted", "dependents": 0})
            if parts[1:] == ["publish"]:
                entry.update(review="pending")
            elif parts[1:] == ["unpublish"]:
                entry.update(review="", visibility="private")
            else:
                if "preset" in doc and entry["form"] != "light":
                    return self._error(400, "preset есть только у настройки поверх модели")
                entry.update({k: v for k, v in doc.items() if k in (
                    "price", "summary", "preset", "min_replicas", "max_replicas")})
            return self._send(200, entry)

        def _layers(self, method, path, body):
            """Движки и расширения автора: `/api/layers` как у оркестратора."""
            doc = json.loads(body) if body else {}
            layers = state.__dict__.setdefault("layers", [])
            parts = [p for p in path.split("/") if p][2:]
            if not parts:
                if method == "GET":
                    return self._send(200, {"layers": layers, "handle": state.handle})
                if not state.handle:
                    return self._error(409, "сначала задайте имя автора: слои зовутся "
                                            "`автор/слой`")
                if len(doc.get("commit") or "") != 40:
                    return self._error(400, "commit: коммит целиком, 40 знаков")
                kind = doc.get("kind") or "engine"
                name = "upper" if kind == "extension" else "echo"
                ref = f"{state.handle}/{name}"
                layer = next((x for x in layers if x["ref"] == ref), None)
                if layer is None:
                    layer = {"ref": ref, "kind": kind, "title": "Учебный движок",
                             "public": False, "approved": None, "versions": []}
                    layers.append(layer)
                number = len(layer["versions"]) + 1
                layer["versions"].insert(0, {
                    "number": number, "status": "pending", "note": "", "files": 2,
                    "source": {"repo": doc["repo"], "commit": doc["commit"],
                               "path": doc.get("path", "")},
                    "extends": f"{state.handle}/echo" if kind == "extension" else None})
                return self._send(200, {**layer, "submitted": number})
            layer = next((x for x in layers if x["ref"] == "/".join(parts[:2])), None)
            if layer is None:
                return self._error(404, f"слоя {'/'.join(parts[:2])} у вас нет")
            if doc.get("public") and layer["approved"] is None:
                return self._error(409, "нет одобренной версии: открывать нечего")
            layer["public"] = bool(doc.get("public"))
            return self._send(200, layer)

        def _dispatch(self, method, path, query, body):
            if path == "/api/me" and method == "PATCH":
                if state.handle:
                    return self._error(409, "имя автора уже задано")
                state.handle = json.loads(body)["handle"]
                return self._send(200, {"id": 1, "handle": state.handle})
            if path == "/api/me":
                return self._send(200, {"id": 1, "email": "author@example.ru", "role": "client",
                                        "project_id": 7, "scopes": ["read", "deploy"]})
            if path == "/api/services" or path.startswith("/api/services/"):
                return self._entries(method, path, body)
            if path == "/api/layers" or path.startswith("/api/layers/"):
                return self._layers(method, path, body)
            if path == "/api/models/check":
                doc = json.loads(body)
                state.model_checks = getattr(state, "model_checks", []) + [doc]
                if "/" in doc.get("engine", ""):
                    return self._send(200, {"engine": doc["engine"], "model": {}, "flags": {},
                                            "fits": True, "stages": 1, "nodes_fit": 1,
                                            "error": "", "weights": None})
                if "pickle" in doc["repo"]:
                    return self._error(403, "веса лежат только в pickle: нужен safetensors")
                return self._send(200, {"engine": doc.get("engine", "torch"), "model": {},
                                        "flags": {}, "fits": True, "stages": 2, "nodes_fit": 3,
                                        "error": "", "weights": {"repo": doc["repo"], "files": 4,
                                                                 "weights_bytes": 8 * 2 ** 30,
                                                                 "pickles": 0,
                                                                 "remote_code": []}})
            if path == "/v1/adapters":
                return self._send(200, {"object": "list", "data": state.adapters})
            if path == "/v1/models":
                return self._send(200, {"object": "list",
                                        "data": getattr(state, "models", [{"id": "chat-model"}])})
            if path == "/v1/manifests/check":
                # Как у платформы: ошибки — все сразу, модели gpu: — против каталога.
                check = getattr(state, "manifest_check", True)
                if check == "forbidden":
                    return self._error(403, "у ключа нет права manage")
                if not check:
                    return self._error(404, "нет такого пути")
                text = json.loads(body)["manifest"]
                state.checked = getattr(state, "checked", []) + [text]
                if "processes" not in text:
                    return self._send(200, {"ok": False, "warnings": [], "models": [],
                                            "problems": ["processes: хотя бы один процесс"]})
                missing = re.findall(r"model:\s*(missing-[\w-]+)", text)
                return self._send(200, {
                    "ok": True, "problems": [],
                    "warnings": [f"gpu.x.model: {m!r} нет среди доступных моделей: вызов ответит 404"
                                 for m in missing],
                    "models": [{"step": "x", "model": m, "kind": None, "status": "missing"}
                               for m in missing]})
            if path == "/v1/charge":
                state.charges = getattr(state, "charges", []) + [json.loads(body)]
                doc = json.loads(body)
                return self._send(200, {"object": "app.event", "event": doc["event"],
                                        "client": doc["client"], "quantity": doc["quantity"]})
            if path == "/v1/chat/completions":
                doc = json.loads(body)
                state.chat_clients = getattr(state, "chat_clients", []) + [
                    self.headers.get("X-Looma-Client")]
                # Настройку автора зовут полями входа вместо сообщений.
                text = (f"эхо: {doc['messages'][-1]['content']}" if doc.get("messages")
                        else "поля: " + json.dumps(doc.get("input"), ensure_ascii=False))
                state.chats = getattr(state, "chats", []) + [doc]
                if doc.get("stream"):
                    events = b"".join(
                        b"data: " + json.dumps({"choices": [{"delta": {"content": piece}}]},
                                               ensure_ascii=False).encode() + b"\n\n"
                        for piece in (text[:4], text[4:])) + b"data: [DONE]\n\n"
                    return self._send(200, raw=events, kind="text/event-stream")
                return self._send(200, {"choices": [{"message": {"role": "assistant",
                                                                 "content": text}}]})
            if path == "/v1/embeddings":
                doc = json.loads(body)
                state.embeds = getattr(state, "embeds", []) + [doc]
                vectors = [[float(len(text)), 0.5] for text in doc["input"]]
                data = [{"object": "embedding", "index": i,
                         "embedding": base64.b64encode(struct.pack("<2f", *v)).decode()}
                        for i, v in reversed(list(enumerate(vectors)))]
                return self._send(200, {"object": "list", "data": data,
                                        "usage": {"prompt_tokens": 3, "total_tokens": 3}})
            if path == "/v1/rerank":
                doc = json.loads(body)
                state.reranks = getattr(state, "reranks", []) + [doc]
                order = sorted(range(len(doc["documents"])),
                               key=lambda i: -len(doc["documents"][i]))
                results = [{"index": i, "relevance_score": 1.0 / (rank + 1)}
                           for rank, i in enumerate(order)][:doc.get("top_n") or None]
                return self._send(200, {"results": results,
                                        "usage": {"prompt_tokens": 9, "total_tokens": 9}})
            if path in ("/v1/images/generations", "/v1/images/edits"):
                doc = json.loads(body)
                state.images = getattr(state, "images", []) + [(path, doc)]
                state.image_clients = getattr(state, "image_clients", []) + [
                    self.headers.get("X-Looma-Client")]
                return self._send(200, {"data": [{"b64_json": base64.b64encode(PNG).decode()}]
                                        * doc.get("n", 1)})
            if path == "/v1/videos" and method == "POST":
                state.videos = getattr(state, "videos", []) + [
                    (json.loads(body), self.headers.get("X-Looma-Client"))]
                return self._send(200, {"id": "video_1", "object": "video", "status": "queued"})
            if path == "/v1/videos/video_1":
                return self._send(200, {"id": "video_1", "status": "completed", "progress": 100})
            if path == "/v1/videos/video_1/content":
                host = self.headers.get("Host")
                return self._send(302, {}, headers={"Location": f"http://{host}/files/video.mp4"})
            match = re.match(r"^/v1/apps(?:/([^/]+))?(?:/(.*))?$", path)
            if not match:
                return self._error(404, "нет такого пути")
            name, rest = (unquote(match.group(1)) if match.group(1) else None), match.group(2) or ""
            if name is None:
                if method == "GET":
                    return self._send(200, {"object": "list",
                                            "data": [_public(a) for a in state.apps.values()]})
                doc = json.loads(body)
                if state.app(doc["name"]):
                    return self._error(409, f"приложение {doc['name']} уже есть")
                app = {"id": f"app-{len(state.apps) + 1}", "object": "app", "name": doc["name"],
                       "slug": doc["name"], "project_id": 7, "staging_release": None,
                       "prod_release": None, "created_at": int(time.time()), "releases": [],
                       "secrets": {}, "history": []}
                state.apps[app["id"]] = app
                return self._send(201, _public(app))
            app = state.app(name)
            if app is None:
                return self._error(404, f"приложения {name} нет")
            if rest == "" and method == "GET":
                return self._send(200, _public(app) | {"environments": self._envs(app),
                                                       "deploys": app["history"]})
            if rest == "preview" or rest.startswith("preview/"):
                where = query.get("looma_environment", "prod")
                status = 500 if "/boom" in path else 200
                return self._send(status, raw=f"{where}: {rest[len('preview'):] or '/'}".encode(),
                                  kind="text/plain")
            if rest == "" and method == "DELETE":
                del state.apps[app["id"]]
                return self._send(200, {"id": app["id"], "deleted": True})
            if rest == "releases" and method == "POST":
                number = len(app["releases"]) + 1
                state.archives[(app["name"], number)] = body
                app["releases"].append({"number": number, "status": "building", "polls": 0,
                                        "error": "", "created_at": int(time.time())})
                return self._send(202, {"number": number, "status": "building"})
            if rest == "releases":
                return self._send(200, {"object": "list",
                                        "data": [self._rel(r) for r in reversed(app["releases"])]})
            found = re.match(r"^releases/(\d+)(/log)?$", rest)
            if found:
                r = app["releases"][int(found.group(1)) - 1]
                if found.group(2):
                    return self._send(200, raw=b"step 1\nstep 2\nERROR: pip failed\n",
                                      kind="text/plain")
                r["polls"] += 1
                if r["status"] == "building" and r["polls"] >= 2:
                    if state.fail_build:
                        r.update(status="failed", error="сборка не удалась: код 1")
                    else:
                        r["status"] = "ready"
                        app["staging_release"] = r["number"]
                        app["history"].insert(0, {"environment": "staging", "action": "deploy",
                                                  "release": r["number"]})
                return self._send(200, self._rel(r))
            if rest == "environments":
                return self._send(200, {"object": "list", "data": self._envs(app)})
            if rest == "promote":
                doc = json.loads(body or b"{}")
                app["prod_release"] = doc.get("release") or app["staging_release"]
                app["history"].insert(0, {"environment": "prod", "action": "promote",
                                          "release": app["prod_release"]})
                return self._send(200, _public(app))
            if rest == "rollback":
                if not app["prod_release"] or app["prod_release"] <= 1:
                    return self._error(409, "откатывать некуда")
                app["prod_release"] -= 1
                return self._send(200, _public(app))
            if rest == "stop":
                app["staging_release"] = app["prod_release"] = None
                return self._send(200, _public(app))
            if rest == "processes":
                env = query.get("environment")
                rows = [p for p in self._procs(app) if env in (None, p["environment"])]
                return self._send(200, {"object": "list", "data": rows})
            found = re.match(r"^processes/([^/]+)/(logs|restart)$", rest)
            if found:
                if found.group(2) == "logs":
                    state.log_reads = getattr(state, "log_reads", 0) + 1
                    lines = [f"строка {i}" for i in range(1, 2 + state.log_reads)]
                    return self._send(200, raw=("\n".join(lines) + "\n").encode(),
                                      kind="text/plain")
                env = query.get("environment", "prod")
                rows = [p for p in self._procs(app) if p["environment"] == env
                        and p["process"] == found.group(1)]
                if not rows:
                    return self._error(404, f"у приложения {app['name']} на {env} нет "
                                            f"запущенного процесса {found.group(1)}")
                return self._send(200, {"object": "list", "data": rows})
            if rest == "secrets" and method == "PUT":
                for k, v in json.loads(body).items():
                    if v is None:
                        app["secrets"].pop(k, None)
                    else:
                        app["secrets"][k] = v
                return self._send(200, {"object": "list", "data": self._secrets(app)})
            if rest == "secrets":
                return self._send(200, {"object": "list", "data": self._secrets(app)})
            if rest == "dev" and method == "POST":
                state.dev_texts.append(json.loads(body)["manifest"])
                models = ["chat-model"]
                return self._send(200, {"object": "app.dev", "app": app["name"],
                                        "app_id": app["id"], "token": DEV_TOKEN,
                                        "expires_at": int(time.time()) + 3600, "models": models,
                                        "quota": {"daily_calls": 200, "used_today": 3},
                                        "manifest": state.dev_manifest,
                                        "dockerfile": state.dev_dockerfile})
            if rest == "stats":
                return self._send(200, {
                    "object": "app.stats", "app": app["name"], "days": int(query.get("days", 30)),
                    "totals": {"requests": 6, "clients": 2, "new_clients": 1, "active_clients": 2,
                               "events": 3, "model_calls": 2, "tokens_in": 1000, "tokens_out": 500,
                               "images": 2, "video_seconds": 0, "cost": 30, "currency": "RUB",
                               "unpriced": ["Qwen-Image-2.1"], "dev_model_calls": 3},
                    "channels": {"web": 3, "api": 1, "mcp": 1, "telegram": 1},
                    "events": [{"name": "answer", "title": "Ответ", "quantity": 3, "clients": 2}],
                    "models": [{"model": "chat", "calls": 1, "tokens_in": 1000, "tokens_out": 500,
                                "images": 0, "video_seconds": 0, "cost": 30},
                               {"model": "Qwen-Image-2.1", "calls": 1, "tokens_in": 0,
                                "tokens_out": 0, "images": 2, "video_seconds": 0, "cost": None}],
                    "top_clients": [{"client": "cl_bob", "events": 2, "model_calls": 1}],
                    "daily": []})
            if rest == "listing":
                listing = app.setdefault("listing", {"object": "app.listing", "id": "lst-1",
                                                     "app": app["name"], "state": "none",
                                                     "live_version": None, "pending": None,
                                                     "url": None, "reason": "", "versions": []})
                if method == "POST":
                    if not app["prod_release"]:
                        return self._error(409, "на prod ничего нет: `looma promote`")
                    number = len(listing["versions"]) + 1
                    listing["versions"].insert(0, {"number": number, "status": "pending",
                                                   "reason": ""})
                    listing.update(state="listed", pending=number)
                elif method == "DELETE":
                    listing.update(state="unlisted", pending=None)
                return self._send(200, listing)
            if rest == "dev" and method == "DELETE":
                return self._send(200, {"object": "app.dev", "app": app["name"], "revoked": 2})
            return self._error(404, "нет такого пути")

        @staticmethod
        def _rel(r):
            return {k: r[k] for k in ("number", "status", "error", "created_at")}

        @staticmethod
        def _secrets(app):
            return [{"name": k, "updated_at": int(time.time())} for k in sorted(app["secrets"])]

        @staticmethod
        def _procs(app):
            out = []
            if app["prod_release"]:
                out.append({"environment": "prod", "process": "web", "kind": "web",
                            "instance": 0, "release": app["prod_release"], "state": "running",
                            "restarts": 0, "last_exit": ""})
            return out

        def _envs(self, app):
            return [{"environment": env, "release": app[f"{env}_release"],
                     "state": "ok" if app[f"{env}_release"] else "stopped",
                     "url": f"https://{app['slug']}{'' if env == 'prod' else '-staging'}"
                            ".apps.example.ru/",
                     "processes": [p for p in self._procs(app) if p["environment"] == env]}
                    for env in ("prod", "staging")]

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

        def do_PATCH(self):
            self._route("PATCH")

    return Handler


def start():
    state = State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def archive_names(data: bytes):
    with tarfile.open(fileobj=io.BytesIO(data)) as handle:
        return sorted(handle.getnames())
