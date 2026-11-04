"""MCP-сервер Looma Float: сервисы из Claude Code, Cursor и других агентов.

    claude mcp add looma -- looma mcp
    # Cursor: ~/.cursor/mcp.json → {"mcpServers": {"looma": {"command": "looma", "args": ["mcp"]}}}

Протокол — JSON-RPC 2.0 построчно через stdin/stdout (транспорт stdio MCP);
в stdout — только сообщения протокола, всё остальное — в stderr. Ключ — тот
же, что у CLI (`looma login` или `LOOMA_API_KEY`).

Правило для агента — в `instructions`: проверь перед записью, проверь после
записи. Перед деплоем, переводом и откатом — `looma_status`; после — запрос
к адресу (`looma_check`) и лог, если что-то не так.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, Optional

from loomafloat import __version__, config, ops
from loomafloat.client import Looma
from loomafloat.http import LoomaError
from loomafloat.pack import PackError

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """Looma Float размещает сервисы с GPU-моделями. У сервиса (аргумент app)
два окружения: staging (сюда встаёт каждый новый релиз) и prod (сюда — только
переводом).

Правила:
1. Проверь перед записью: перед looma_deploy, looma_promote, looma_rollback,
   looma_stop и looma_secrets_set вызови looma_status — пойми, что сейчас
   запущено и на каком релизе.
2. Проверь после записи: после looma_deploy открой staging через looma_check;
   после looma_promote и looma_rollback — prod. Не отвечает или ошибка —
   looma_logs нужного процесса и окружения, и только потом выводы.
3. На prod — только когда staging проверен, или когда пользователь просит
   явно. looma_destroy — только по прямой просьбе пользователя, это
   необратимо (база и бакет уходят вместе с сервисом).
4. Значения секретов не пересказывай и не пиши в файлы проекта: только
   looma_secrets_set. Процессы видят новые секреты после перезапуска.
5. Сборка не удалась — причина в log_tail результата looma_deploy; поправь
   проект и деплой снова. Модели сервис зовёт только те, что объявлены в
   gpu: манифеста looma.yaml (иначе 403).
6. Запустить у себя до деплоя — `looma dev` в терминале пользователя: окружение
   как на платформе, база и бакет в Docker, секреты — в .env проекта.
7. Перенос чужого проекта на площадку — по skill looma-port: looma_assess
   (что мешает), план пользователю, правки, looma_validate (манифест без
   релиза), looma_deploy, looma_check, отчёт. Модели для gpu: — из looma_models.
"""


def _app_prop():
    return {"type": "string", "description": "имя сервиса (как в looma.yaml) или его id"}


ENV = {"type": "string", "enum": ["prod", "staging"], "description": "окружение"}

TOOLS = [
    {"name": "looma_whoami", "description": "Кто я: учётная запись, проект, права ключа.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_apps", "description": "Мои сервисы с релизами окружений и адресами.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_status",
     "description": "Состояние сервиса: окружения (релиз, состояние, адрес), процессы, "
                    "сертификат, последние переключения. Вызывать перед любым изменением.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_init",
     "description": "Создать проект сервиса из шаблона в папке на диске.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка проекта (будет создана)"},
         "template": {"type": "string", "description": "шаблон",
                      "enum": ops.templates()},
         "name": {"type": "string", "description": "имя сервиса (латиница, цифры, дефис)"}},
         "required": ["directory"]}},
    {"name": "looma_assess",
     "description": "Оценить проект в папке: подойдёт ли площадке, что мешает (блокеры), что "
                    "переделать, с местами файл:строка; черновик looma.yaml. Ключ не нужен.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка проекта"}},
         "required": ["directory"]}},
    {"name": "looma_validate",
     "description": "Проверить looma.yaml и проект без релиза: все ошибки манифеста сразу, "
                    "модели из gpu: (есть ли в каталоге, спят ли), иконка, Dockerfile, архив. "
                    "Секунды вместо сборки.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка с looma.yaml"}},
         "required": ["directory"]}},
    {"name": "looma_models",
     "description": "Модели каталога: имя для gpu: манифеста, тип (text, image, video, "
                    "embedding, rerank), отвечает или спит, контекст.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_deploy",
     "description": "Собрать релиз из папки с looma.yaml и поставить на staging (promote=true — "
                    "и сразу на prod). Ждёт сборки; при отказе — причина и хвост лога. "
                    "Минуты.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка с looma.yaml"},
         "app": {"type": "string", "description": "имя, если не то, что в looma.yaml"},
         "promote": {"type": "boolean", "default": False}},
         "required": ["directory"]}},
    {"name": "looma_releases", "description": "Релизы сервиса: номер, статус, ошибка.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_logs",
     "description": "Лог процесса: идущего — с узла, упавшего — сохранённый хвост.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "process": {"type": "string", "description": "процесс (web, worker…)"},
         "environment": ENV, "tail": {"type": "integer", "default": 100}},
         "required": ["app"]}},
    {"name": "looma_check",
     "description": "Запрос к вебу сервиса и код, время, начало ответа — проверка после "
                    "записи. По умолчанию через API владельца: доходит до процесса и при "
                    "access: clients. public=true — по публичному адресу, как гость (шлюз, "
                    "сертификат, вход). Спящий веб просыпается этим запросом (до двух минут).",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "environment": ENV,
         "path": {"type": "string", "default": "/", "description": "путь на сайте сервиса"},
         "public": {"type": "boolean", "default": False}},
         "required": ["app"]}},
    {"name": "looma_promote", "description": "На prod — релиз staging (или названный).",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "release": {"type": "integer"}}, "required": ["app"]}},
    {"name": "looma_rollback",
     "description": "Окружение (по умолчанию prod) — на прошлый релиз.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop(), "environment": ENV},
                     "required": ["app"]}},
    {"name": "looma_stop", "description": "Остановить окружение (без environment — оба).",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop(), "environment": ENV},
                     "required": ["app"]}},
    {"name": "looma_restart",
     "description": "Перезапустить процесс (подхватит новые секреты); cron — запустить сейчас.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "process": {"type": "string"}, "environment": ENV},
         "required": ["app", "process"]}},
    {"name": "looma_secrets_list", "description": "Имена секретов (значения не отдаются).",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_secrets_set",
     "description": "Поставить секреты {ИМЯ: значение}; null — убрать.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(),
         "values": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}},
         "required": ["app", "values"]}},
    {"name": "looma_stats",
     "description": "Статистика сервиса за days дней: запросы по каналам, клиенты, события "
                    "charge, вызовы моделей и себестоимость по прайсу.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "days": {"type": "integer", "default": 30}}, "required": ["app"]}},
    {"name": "looma_publish",
     "description": "Опубликовать карточку сервиса в маркетплейсе: берутся разделы listing "
                    "и events (цены) из looma.yaml релиза на prod, версия уходит на "
                    "модерацию. Только по просьбе пользователя — карточку увидят все.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_listing",
     "description": "Карточка в маркетплейсе: в каталоге ли, что на модерации, причина отказа.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_unpublish",
     "description": "Снять карточку из каталога (вернуть — looma_publish, снова через "
                    "модерацию). Только по просьбе пользователя.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_destroy",
     "description": "Удалить сервис со всеми данными. Необратимо: только по прямой просьбе "
                    "пользователя; confirm — имя сервиса ещё раз.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "confirm": {"type": "string"}}, "required": ["app", "confirm"]}},
]


class Server:
    def __init__(self, looma: Optional[Looma] = None, out=None) -> None:
        self._looma = looma
        self.out = out or sys.stdout
        self._progress_token = None
        self._progress_n = 0
        self.handlers: Dict[str, Callable] = {t["name"]: getattr(self, t["name"]) for t in TOOLS}

    @property
    def looma(self) -> Looma:
        if self._looma is None:
            self._looma = Looma()
            self._looma.key = config.user_key()
        if not self._looma.key:
            raise ops.OpError("нет ключа Looma Float: `looma login` в терминале или LOOMA_API_KEY")
        return self._looma

    # -------------------------------------------------------- протокол
    def send(self, message: dict) -> None:
        self.out.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.out.flush()

    def handle(self, message: dict) -> Optional[dict]:
        method = message.get("method")
        ident = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return self._ok(ident, {
                "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "looma", "version": __version__},
                "instructions": INSTRUCTIONS})
        if method == "ping":
            return self._ok(ident, {})
        if method == "tools/list":
            return self._ok(ident, {"tools": TOOLS})
        if method == "tools/call":
            return self._ok(ident, self.call(params.get("name"), params.get("arguments") or {},
                                             (params.get("_meta") or {}).get("progressToken")))
        if ident is None:              # уведомление (notifications/initialized и пр.)
            return None
        return {"jsonrpc": "2.0", "id": ident,
                "error": {"code": -32601, "message": f"метода {method!r} нет"}}

    @staticmethod
    def _ok(ident, result) -> dict:
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def call(self, name: str, arguments: dict, progress=None) -> dict:
        handler = self.handlers.get(name)
        if handler is None:
            return self._result(f"инструмента {name!r} нет", error=True)
        self._progress_token = progress
        self._progress_n = 0
        try:
            return self._result(handler(**arguments))
        except TypeError as exc:
            return self._result(f"неверные аргументы {name}: {exc}", error=True)
        except (LoomaError, ops.OpError, PackError) as exc:
            return self._result(getattr(exc, "message", None) or str(exc), error=True)
        except Exception as exc:      # агент должен увидеть причину, а не обрыв
            traceback.print_exc(file=sys.stderr)
            return self._result(f"{type(exc).__name__}: {exc}", error=True)

    @staticmethod
    def _result(value, error: bool = False) -> dict:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                                indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": error}

    def _progress(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)
        if self._progress_token is not None:
            self._progress_n += 1
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": self._progress_token,
                                  "progress": self._progress_n, "message": text}})

    # -------------------------------------------------------- инструменты
    def looma_whoami(self):
        me = self.looma.whoami()
        return {k: me.get(k) for k in ("email", "role", "project_id", "project_role", "scopes")}

    def looma_apps(self):
        return [{k: a.get(k) for k in ("name", "id", "staging_release", "prod_release", "urls")}
                for a in self.looma.apps.list()]

    def looma_status(self, app: str):
        doc = self.looma.apps.get(app)
        return {"name": doc["name"], "id": doc["id"], "urls": doc.get("urls"),
                "tls": doc.get("tls"), "environments": doc.get("environments"),
                "recent_switches": (doc.get("deploys") or [])[:5],
                "releases": [{k: r.get(k) for k in ("number", "status", "error")}
                             for r in (doc.get("releases") or [])[:5]],
                "secrets": doc.get("secrets"), "models": (doc.get("token") or {}).get("models")}

    def looma_init(self, directory: str, template: str = "hello", name: Optional[str] = None):
        return ops.init(Path(directory), template, name)

    def looma_assess(self, directory: str):
        report = ops.assess(Path(directory))
        report.pop("markdown", None)
        return report

    def looma_validate(self, directory: str):
        looma = self._looma
        if looma is None:
            looma = Looma()
            looma.key = config.user_key()
        return ops.validate(looma if looma.key else None, Path(directory))

    def looma_models(self):
        return [{k: m.get(k) for k in ("id", "kind", "status", "context", "dimensions")
                 if m.get(k) is not None} for m in self.looma.models()]

    def looma_deploy(self, directory: str, app: Optional[str] = None, promote: bool = False):
        return ops.deploy(self.looma, Path(directory), app=app, promote=promote,
                          on_progress=self._progress)

    def looma_releases(self, app: str):
        return [{k: r.get(k) for k in ("number", "status", "error", "created_at")}
                for r in self.looma.apps.releases(app)]

    def looma_logs(self, app: str, process: Optional[str] = None, environment: str = "prod",
                   tail: int = 100):
        if not process:
            names = [p["process"] for p in self.looma.apps.processes(app, environment)]
            if not names:
                return (f"на {environment} нет запущенных процессов: веб мог заснуть — "
                        "looma_check разбудит его")
            process = "web" if "web" in names else names[0]
        return self.looma.apps.logs(app, process, environment=environment,
                                    tail=max(1, min(int(tail), 2000)))

    def looma_check(self, app: str, environment: str = "staging", path: str = "/",
                    public: bool = False):
        if public:
            return ops.check(ops.app_url(self.looma, app, environment), path=path)
        return ops.check_app(self.looma, app, environment, path)

    def looma_promote(self, app: str, release: Optional[int] = None):
        doc = self.looma.apps.promote(app, release)
        return {"prod_release": doc.get("prod_release"), "prod_url": (doc.get("urls") or {})
                .get("prod"), "next": "проверь prod: looma_check с environment=prod"}

    def looma_rollback(self, app: str, environment: str = "prod"):
        doc = self.looma.apps.rollback(app, environment)
        return {f"{environment}_release": doc.get(f"{environment}_release"),
                "next": f"проверь {environment}: looma_check"}

    def looma_stop(self, app: str, environment: Optional[str] = None):
        doc = self.looma.apps.stop(app, environment)
        return {"staging_release": doc.get("staging_release"),
                "prod_release": doc.get("prod_release")}

    def looma_restart(self, app: str, process: str, environment: str = "prod"):
        try:
            return self.looma.apps.restart(app, process, environment)
        except LoomaError as exc:
            if exc.status == 404 and process == "web":
                return "web спит — проснётся первым запросом (looma_check), уже с новыми секретами"
            raise

    def looma_secrets_list(self, app: str):
        return [row["name"] for row in self.looma.apps.secrets(app)]

    def looma_secrets_set(self, app: str, values: dict):
        if not isinstance(values, dict) or not values:
            raise ops.OpError("values — {ИМЯ: значение}")
        rows = self.looma.apps.set_secrets(app, values)
        return {"secrets": [row["name"] for row in rows],
                "next": "процессы увидят новые значения после looma_restart"}

    def looma_stats(self, app: str, days: int = 30):
        doc = self.looma.apps.stats(app, days=max(1, min(int(days), 90)))
        return {k: doc[k] for k in ("app", "days", "totals", "channels", "events", "models",
                                    "top_clients")}

    def looma_publish(self, app: str):
        state = ops.listing_state(self.looma.apps.publish(app))
        state["next"] = "ответ модератора — looma_listing"
        return state

    def looma_listing(self, app: str):
        return ops.listing_state(self.looma.apps.listing(app))

    def looma_unpublish(self, app: str):
        return ops.listing_state(self.looma.apps.unpublish(app))

    def looma_destroy(self, app: str, confirm: str):
        if confirm != app:
            raise ops.OpError("confirm должен повторять имя сервиса: удаление необратимо")
        self.looma.apps.delete(app)
        return f"сервис {app} удалён вместе с базой, бакетом и релизами"


def serve(stdin=None, stdout=None, looma: Optional[Looma] = None) -> None:
    server = Server(looma, stdout)
    for line in stdin or sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            server.send({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "не JSON"}})
            continue
        batch = message if isinstance(message, list) else [message]
        for item in batch:
            answer = server.handle(item) if isinstance(item, dict) else None
            if answer is not None:
                server.send(answer)


__all__ = ["INSTRUCTIONS", "Server", "TOOLS", "serve"]
