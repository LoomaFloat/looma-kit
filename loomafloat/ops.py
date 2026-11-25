"""Действия над сервисами, общие для CLI и MCP: результат — данными, а
не печатью, чтобы одно и то же показывать человеку и отдавать агенту."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Optional, Tuple

from loomafloat.client import Looma
from loomafloat.http import LoomaError
from loomafloat.pack import (MANIFEST, PackError, ignored, manifest_name, pack, pack_engine,  # noqa: F401
                             patterns)

TEMPLATES = Path(__file__).with_name("templates")
#: Шаблону нужен SDK — копия ложится в проект: сервис собирается на
#: платформе без PyPI и без доступа к репозиторию.
VENDOR_MARKER = ".vendor-loomafloat"
VENDORED = ("__init__.py", "client.py", "config.py", "http.py")


class OpError(RuntimeError):
    """Действие не выполнить — словами."""


def templates() -> list:
    return sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir())


def init(target: Path, template: str = "hello", name: Optional[str] = None) -> dict:
    source = TEMPLATES / template
    if template not in templates():
        raise OpError(f"шаблона {template!r} нет; есть: {', '.join(templates())}")
    target = Path(target).resolve()
    if (target / MANIFEST).exists():
        raise OpError(f"в {target} уже есть {MANIFEST}")
    name = (name or target.name).lower()
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.relative_to(source).parts:
            continue  # кэш Python остаётся от проверки шаблона, в проект он не нужен
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if path.name == VENDOR_MARKER:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copy2(path, destination)
        else:
            destination.write_text(text.replace("{{name}}", name), encoding="utf-8")
        written.append(destination.relative_to(target).as_posix())
    if (source / VENDOR_MARKER).exists():
        written += vendor(target)
    return {"directory": str(target), "name": name, "template": template, "files": written}


def vendor(target: Path) -> list:
    """Свежая копия SDK в папке `loomafloat/` проекта: процессы импортируют её
    без установки пакета. Заменяет только модули SDK, остальное в папке не трогает."""
    sdk = Path(target) / "loomafloat"
    sdk.mkdir(parents=True, exist_ok=True)
    for module in VENDORED:
        shutil.copy2(Path(__file__).with_name(module), sdk / module)
    return [f"loomafloat/{module}" for module in VENDORED]


_IMPORTS_SDK = re.compile(r"^[ \t]*(?:from[ \t]+loomafloat\b|import[ \t]+loomafloat\b)", re.MULTILINE)


def missing_sdk(root: Path) -> bool:
    """Код импортирует loomafloat, а взять его процессу негде: копии в проекте
    нет, в зависимостях пакета тоже. Сборка на платформе такой релиз примет, а
    процесс упадёт на первом же import — так бывает, когда папку SDK удалили
    или забыли `looma sdk` в новом проекте."""
    root = Path(root)
    if (root / "loomafloat" / "__init__.py").exists():
        return False
    for name in ("requirements.txt", "pyproject.toml"):
        try:
            if "loomafloat" in (root / name).read_text(encoding="utf-8", errors="replace"):
                return False
        except OSError:
            pass
    rules = patterns(root)
    for top, dirs, names in os.walk(root):
        here = PurePosixPath(Path(top).relative_to(root).as_posix())
        dirs[:] = [d for d in dirs if not ignored(here / d, rules)]
        for name in names:
            if not name.endswith(".py") or ignored(here / name, rules):
                continue
            try:
                text = (Path(top) / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _IMPORTS_SDK.search(text):
                return True
    return False


# ---------------------------------------------------------------- skills
#: Skills для агентов: с чего начать (looma), перенос проекта в сервис, работа с
#: сервисом, свой движок на видеокарте.
SKILLS = ("looma", "looma-port", "looma-apps", "looma-engine")

#: Куда агенты читают skills: (у пользователя, в проекте). Пути — из
#: документации агентов; `agents` — общий каталог открытого стандарта.
SKILL_DIRS = {
    "claude": ("~/.claude/skills", ".claude/skills"),
    "cursor": ("~/.cursor/skills", ".cursor/skills"),
    "codex": ("~/.agents/skills", ".agents/skills"),
    "agents": ("~/.agents/skills", ".agents/skills"),
    "copilot": ("~/.copilot/skills", ".github/skills"),
    "gemini": ("~/.gemini/skills", ".gemini/skills"),
}


def skills_root() -> Path:
    """Папка skills: в установленном пакете — рядом с модулями, в клоне
    репозитория — в его корне."""
    inside = Path(__file__).with_name("skills")
    return inside if inside.is_dir() else Path(__file__).resolve().parent.parent / "skills"


def skill_target(agent: str, project: Optional[Path] = None) -> Path:
    if agent not in SKILL_DIRS:
        raise OpError(f"агент {agent!r}: бывают {', '.join(SKILL_DIRS)}")
    user, local = SKILL_DIRS[agent]
    if project is not None:
        return Path(project).resolve() / local
    if agent == "claude" and os.environ.get("CLAUDE_CONFIG_DIR"):
        return Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills"
    return Path(user).expanduser()


def install_skills(agent: str, project: Optional[Path] = None,
                   names: tuple = SKILLS) -> list:
    """Скопировать skills туда, где агент их читает. Свою папку skill
    заменяет целиком (старые файлы справки не остаются), чужие не трогает."""
    target = skill_target(agent, project)
    source = skills_root()
    placed = []
    for name in names:
        if not (source / name / "SKILL.md").is_file():
            raise OpError(f"skill {name} не найден в {source}")
        destination = target / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source / name, destination,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        placed.append(str(destination))
    return placed


# ---------------------------------------------------------- оценка, проверка
def assess(root: Path) -> dict:
    """Оценка проекта скриптом skill looma-port: тем же, что агент запускает
    сам без установки CLI. Результат — данными и отчётом в Markdown."""
    root = Path(root)
    if not root.is_dir():
        raise OpError(f"нет папки {root}")
    script = skills_root() / "looma-port" / "scripts" / "assess.py"
    spec = importlib.util.spec_from_file_location("looma_assess", script)
    if spec is None or spec.loader is None:
        raise OpError(f"нет скрипта оценки: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    report = module.assess(root)
    report["markdown"] = module.markdown(report)
    return report


_ICON = re.compile(r"^\s+icon:\s*['\"]?([^'\"#\s]+)", re.M)
_DOCKERFILE = re.compile(r"dockerfile:\s*['\"]?([^'\"},\s]+)")
ICON_MAX = 256 * 1024


def validate(looma: Optional[Looma], root: Path) -> dict:
    """Проверить проект перед релизом: манифест — на платформе (все ошибки
    сразу, модели — против тех, что отвечают), файлы — здесь: иконка,
    Dockerfile, архив, SDK. Без ключа — только то, что видно здесь."""
    root = Path(root).resolve()
    path = root / MANIFEST
    if not path.is_file():
        raise OpError(f"в {root} нет {MANIFEST}: черновик манифеста — в отчёте `looma assess`")
    text = path.read_text(encoding="utf-8")
    problems, warnings, models = [], [], []
    icon = _ICON.search(text)
    if icon:
        file = root / icon.group(1)
        if not file.is_file():
            problems.append(f"listing.icon: нет файла {icon.group(1)}")
        elif file.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            problems.append("listing.icon: PNG, JPEG или WebP")
        elif file.stat().st_size > ICON_MAX:
            problems.append(f"listing.icon: {file.stat().st_size // 1024} КБ, можно до 256 КБ")
    if re.search(r"kind:\s*['\"]?docker\b", text):
        found = _DOCKERFILE.search(text)
        name = found.group(1) if found else "Dockerfile"
        if not (root / name).is_file():
            problems.append(f"runtime.dockerfile: нет файла {name}")
    try:
        _archive, files, _skipped = pack(root)
        archive = {"files": len(files), "bytes": len(_archive)}
    except PackError as exc:
        problems.append(str(exc))
        archive = None
    if missing_sdk(root):
        warnings.append("код импортирует loomafloat, а копии SDK в проекте нет: "
                        "`looma deploy` положит её сам (или `looma sdk`)")
    checked = None
    if looma is not None and looma.key:
        try:
            checked = looma.apps.check_manifest(text)
        except LoomaError as exc:
            # 404: проверки на площадке ещё нет. 403: площадка старой версии
            # просит для неё полное право manage, которого у ключа из
            # `looma login` нет. В обоих случаях манифест проверит сборка.
            if exc.status in (403, 404):
                warnings.append("площадка не проверила манифест без релиза: ошибки покажет "
                                "сборка (`looma deploy`)")
            else:
                raise
    else:
        warnings.append("без входа манифест на платформе не проверен: `looma login`")
    if checked:
        problems = list(checked.get("problems") or []) + problems
        warnings = list(checked.get("warnings") or []) + warnings
        models = checked.get("models") or []
    return {"ok": not problems, "problems": problems, "warnings": warnings, "models": models,
            "archive": archive}


STATES = {"live": "в каталоге", "pending": "ждёт модерации", "rejected": "не в каталоге",
          "unlisted": "снята автором", "blocked": "закрыта модератором", "none": "не публиковалась"}


CHANNEL_NAMES = {"web": "веб", "api": "API", "mcp": "MCP", "telegram": "Telegram"}


def rubles(kopecks) -> str:
    return "—" if kopecks is None else f"{kopecks / 100:,.2f} ₽".replace(",", " ").replace(".", ",")


# ------------------------------------------------- свои модели в каталоге
ENTRY_STATES = {"ready": "отвечает", "starting": "поднимается", "cold": "спит",
                "unavailable": "не отвечает"}
#: Единицы цены модели: имя в CLI → поле API (копейки).
PRICE_UNITS = {"in": "price_in", "out": "price_out", "image": "price_image",
               "second": "price_second", "call": "price_call"}
_UNIT_TITLES = {"price_in": "вход", "price_out": "выход", "price_image": "за изображение",
                "price_second": "за секунду видео", "price_call": "за вызов"}


class EntryError(OpError):
    """Команду о своей модели не выполнить: причина словами."""


def entry_form(entry: dict) -> str:
    """Вид записи: настройка поверх модели, модель с адаптером или свои веса."""
    if entry.get("form") == "light":
        return "настройка"
    recipe = entry.get("recipe") or {}
    if "/" in str(recipe.get("engine") or ""):
        return "движок"                 # запись на движке автора
    return "адаптер" if recipe.get("adapter") else "веса"


def entry_publication(entry: dict) -> str:
    if entry.get("visibility") == "public":
        return "опубликована"
    return {"pending": "на проверке", "rejected": "отклонена"}.get(entry.get("review") or "",
                                                                    "видите только вы")


def entry_price(entry: dict) -> str:
    """Цена словами; у настройки это надбавка к цене её основы."""
    price = entry.get("price") or {}
    parts = [f"{_UNIT_TITLES[unit]} {rubles(price[unit])}" for unit in _UNIT_TITLES
             if price.get(unit)]
    if not parts:
        return "без надбавки" if entry.get("form") == "light" else "бесплатно"
    tokens = " за 1M токенов" if price.get("price_in") or price.get("price_out") else ""
    return ("надбавка: " if entry.get("form") == "light" else "") + ", ".join(parts) + tokens


def entry_row(entry: dict) -> dict:
    """Запись коротко — для списка и для ИИ-агента."""
    row = {"id": entry["id"], "name": entry["label"], "form": entry_form(entry),
           "kind": (entry.get("passport") or {}).get("kind") or "text",
           "status": entry.get("status"), "publication": entry_publication(entry),
           "price": entry.get("price") or {}, "summary": entry.get("summary") or ""}
    if entry.get("form") == "light":
        row["base"] = entry.get("base")
    else:
        row["repo"] = (entry.get("recipe") or {}).get("repo")
        engine = str((entry.get("recipe") or {}).get("engine") or "")
        if "/" in engine:
            row["engine"] = engine
            row["engine_version"] = (entry.get("recipe") or {}).get("engine_version")
        row["replicas"] = {"min": entry.get("min_replicas"), "max": entry.get("max_replicas"),
                           "running": len(entry.get("replicas") or [])}
        # Модель работает только на узлах своего владельца.
        row["own_nodes"] = list((entry.get("passport") or {}).get("contours") or []) == ["own"]
        if (entry.get("recipe") or {}).get("own_node"):
            row["node"] = entry["recipe"]["own_node"]
        if entry.get("engine_update"):
            row["engine_update"] = entry["engine_update"]
    for name in ("note", "review_note"):
        if entry.get(name):
            row[name] = entry[name]
    return row


def price_of(pairs) -> dict:
    """`in=5 out=12.5` (рубли) → цена для API в копейках. Ноль единицу убирает."""
    price = {}
    for pair in pairs or []:
        unit, sep, value = str(pair).partition("=")
        if not sep or unit.strip() not in PRICE_UNITS:
            raise EntryError(f"{pair!r}: цена задаётся как единица=рубли; единицы: "
                             f"{', '.join(PRICE_UNITS)}")
        try:
            kopecks = round(float(value.replace(",", ".")) * 100)
        except ValueError:
            raise EntryError(f"{pair!r}: рубли — число, например out=12.5") from None
        if kopecks < 0:
            raise EntryError(f"{pair!r}: цена не бывает отрицательной")
        if kopecks:
            price[PRICE_UNITS[unit.strip()]] = kopecks
    return price


def recipe_of(pairs) -> dict:
    """`stages=2 dtype=bfloat16` → поля рецепта движка. Числа и да/нет читаются
    по виду, остальное идёт строкой."""
    recipe = {}
    for pair in pairs or []:
        key, sep, value = str(pair).partition("=")
        if not sep or not key.strip():
            raise EntryError(f"{pair!r}: параметр движка задаётся как имя=значение")
        value = value.strip()
        recipe[key.strip()] = (True if value.lower() == "true" else False
                               if value.lower() == "false" else int(value)
                               if re.fullmatch(r"-?\d+", value) else value)
    return recipe


def read_preset(path: Path) -> dict:
    """Настройка из файла JSON: `system`, `template`, `inputs`, `params` у
    текстовой модели; `prompt`, `inputs`, `params` у изображений и видео."""
    try:
        preset = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise EntryError(f"файл настройки {path} не прочитать: {exc.strerror or exc}") from None
    except ValueError as exc:
        raise EntryError(f"файл настройки {path} не JSON: {exc}") from None
    if not isinstance(preset, dict):
        raise EntryError(f"файл настройки {path}: нужен JSON-объект")
    return preset


def entry_payload(*, name: str, repo: str = "", engine: str = "", recipe: Optional[dict] = None,
                  base: str = "", adapter: str = "", preset: Optional[dict] = None,
                  summary: str = "", always: bool = False, own: bool = False,
                  node: str = "", comfy: Optional[dict] = None) -> dict:
    """Тело запроса на новую запись: одно из четырёх — свои веса (`repo`), модель
    каталога с адаптером (`base` и `adapter`), настройка (`base` и `preset`),
    граф ComfyUI (`comfy`: описание и граф из `read_comfy`)."""
    if not name:
        raise EntryError("как назвать модель? Имя: латиница, цифры, точка, дефис")
    if comfy is not None:
        if repo or base or adapter or preset is not None or node or engine not in ("", "comfyui"):
            raise EntryError("модель на ComfyUI задаёт описание графа (--from файл.comfy.yaml): "
                             "repo, base, adapter, preset и свой движок с ним не нужны")
        payload = {"name": name, **({"summary": summary} if summary else {}),
                   "min_replicas": 1 if always else 0, "max_replicas": 1,
                   **({"contours": ["own"]} if own else {}), **(recipe or {}), **comfy}
        return payload
    # Движок автора (`автор/движок`) может работать и без весов.
    authored = "/" in engine and not base
    chosen = [bool(repo) or authored, bool(base and adapter),
              bool(base and preset is not None)]
    if sum(chosen) != 1 or (base and repo):
        raise EntryError("что добавить? Одно из четырёх: --repo владелец/модель (свои веса), "
                         "--base модель --adapter адаптер, --base модель --preset файл.json, "
                         "--from описание.comfy.yaml (граф ComfyUI)")
    payload = {"name": name}
    if summary:
        payload["summary"] = summary
    if preset is not None and base:
        if always or recipe or engine or own or node:
            raise EntryError("у настройки нет своих реплик и движка: она работает на репликах "
                             f"{base}")
        return {**payload, "base": base, "preset": preset}
    payload.update(min_replicas=1 if always else 0, max_replicas=1)
    if own:
        payload["contours"] = ["own"]         # модель встаёт только на свои узлы автора
    if node:
        if "/" not in engine:
            raise EntryError("свой узел называют модели на движке автора (--engine автор/движок)")
        payload["own_node"] = node            # и только на этот свой узел
    if base:
        return {**payload, "base": base, "adapter": adapter}
    return {**payload, **(recipe or {}), **({"repo": repo} if repo else {}),
            **({"engine": engine} if engine else {})}


# --------------------------------------------------------------- ComfyUI
COMFY_GRAPH_LINE = re.compile(r"^graph:[ \t]*(?P<path>[^#\n]*?)[ \t]*(?:#.*)?$", re.M)
MAX_GRAPH_BYTES = 1024 * 1024
COMFY_KINDS = {"image": "изображения: общий запрос /v1/images, витрина и чат",
               "video": "видео: общий запрос /v1/videos",
               "api": "своё API: /v1/run/<модель> и задания"}


def read_graph(path) -> dict:
    """Граф ComfyUI из файла: API-формат («Export (API)»), JSON-объект."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EntryError(f"граф {path} не прочитать: {exc.strerror or exc}") from None
    if len(raw) > MAX_GRAPH_BYTES:
        raise EntryError(f"граф {path.name} больше {MAX_GRAPH_BYTES // 1024} КБ")
    try:
        graph = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise EntryError(f"граф {path.name} не JSON") from None
    if isinstance(graph, dict) and "nodes" in graph and "links" in graph:
        raise EntryError(f"{path.name} — граф в формате редактора; нужен API-формат: в ComfyUI "
                         "меню Workflow → Export (API)")
    if not isinstance(graph, dict) or not graph:
        raise EntryError(f"{path.name}: граф в API-формате — JSON-объект узлов")
    return graph


def read_comfy(path) -> dict:
    """Описание модели на ComfyUI (`*.comfy.yaml`) и граф, на который оно
    ссылается: `{"engine": "comfyui", "description": текст, "graph": граф}`.
    Описание разбирает площадка; здесь из него берётся только путь к графу."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EntryError(f"описание {path} не прочитать: {exc.strerror or exc}") from None
    found = COMFY_GRAPH_LINE.search(text)
    if not found or not found.group("path").strip().strip("\"'"):
        raise EntryError(f"в {path.name} нет строки graph: путь к графу в API-формате, "
                         "например graph: workflow_api.json")
    graph = read_graph(path.parent / found.group("path").strip().strip("\"'"))
    return {"engine": "comfyui", "description": text, "graph": graph}


def read_comfy_text(path) -> str:
    """Текст описания модели на ComfyUI: из него прогону нужны веса."""
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EntryError(f"описание {path} не прочитать: {exc.strerror or exc}") from None


def _flow(value) -> str:
    """Значение описания в строку YAML: словарь и список — в одну строку, строки —
    в кавычках JSON (их YAML читает так же)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_flow(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def comfy_yaml(report: dict, graph_file: str) -> str:
    """Черновик описания модели из разбора графа (`looma comfy inspect --write`)."""
    draft = report.get("draft") or {}
    lines = ["# Описание модели на ComfyUI для Looma Float (looma comfy inspect).",
             "# Проверьте выход и входы; у весов с пустым repo назовите его: открытый",
             "# репозиторий HuggingFace, файл в safetensors (файлы из весов редактора уже",
             "# заполнены). Добавить: looma model add <имя> --from <этот файл>",
             f"graph: {_flow(graph_file)}"]
    if draft.get("extensions"):
        lines.append("# наборы узлов площадки, которыми граф пользуется")
        lines.append(f"extensions: {_flow(draft['extensions'])}")
    if draft.get("output"):
        lines.append(f"output: {_flow(draft['output'])}")
    else:
        lines.append("# output: {node: \"…\"}  — узел сохранения результата")
    inputs = draft.get("inputs") or {}
    lines.append("inputs:" if inputs else "inputs: {}")
    for name, about in inputs.items():
        lines.append(f"  {name}: {_flow(about)}")
    weights = draft.get("weights") or []
    lines.append("weights:" if weights else "weights: []")
    for item in weights:
        lines.append(f"  - {_flow(item)}")
    return "\n".join(lines) + "\n"


def comfy_set_lines(item: dict) -> list:
    """Набор узлов ComfyUI строками (`looma comfy sets`)."""
    lines = [f"{item['ref']}, версия {item.get('version')}: {item.get('title') or ''}".rstrip(": "),
             f"  узлов {len(item.get('nodes') or [])}"
             + (f"; сохраняют результат: {', '.join(f'{n} ({m})' for n, m in item['outputs'].items())}"
                if item.get("outputs") else "")]
    if item.get("folders"):
        lines.append(f"  свои папки весов: {', '.join(item['folders'])}")
    if item.get("suffixes"):
        lines.append(f"  ещё читает веса: {', '.join(item['suffixes'])}")
    if item.get("review") == "own":
        lines.append("  ваша версия без ревью: модель с ней встанет только на ваши узлы")
    return lines


def inspect_lines(report: dict) -> list:
    """Разбор графа строками для человека."""
    lines = [f"ComfyUI {report.get('comfyui')}: узлов в графе {report.get('nodes')}"]
    for item in report.get("extensions") or []:
        lines.append(f"набор узлов: {item['ref']}, версия {item.get('version')} "
                     f"(узлы графа: {', '.join(item.get('used') or [])})")
    if report.get("unknown"):
        lines.append(f"узлов нет ни в ядре, ни в наборах площадки: "
                     f"{', '.join(report['unknown'])}")
    for out in report.get("outputs") or []:
        media = {"image": "изображение", "video": "видео"}.get(out.get("media"), "не результат")
        lines.append(f"выход: узел {out['node']} ({out['class_type']}, {media})")
    for item in report.get("weights") or []:
        found = f", из весов редактора: {item['repo']}/{item['file']}" if item.get("repo") else ""
        lines.append(f"вес: {item['folder']}/{item['name']} (узел {item['node']} "
                     f"{item['class_type']}, поле {item['field']}{found})")
    for name, about in (report.get("inputs") or {}).items():
        where = about.get("field") or "/".join(about.get("fields") or [])
        lines.append(f"вход {name}: узел {about['node']}, поле {where} ({about['type']})")
    kind = report.get("kind") or "api"
    lines.append(f"род модели: {kind}, {COMFY_KINDS.get(kind, '')}")
    for problem in report.get("problems") or []:
        lines.append(f"поправить: {problem}")
    return lines


# ------------------------------------------- прогоны графа без модели
#: Узлы ядра, которые грузят входной файл по имени: такой файл едет с прогоном.
RUN_INPUTS = {"LoadImage": ("image",), "LoadImageMask": ("image",), "LoadVideo": ("file",),
              "LoadAudio": ("audio",)}
MAX_RUN_FILE_BYTES = 32 * 1024 * 1024
RUN_STATES = {"queued": "в очереди", "in_progress": "идёт", "completed": "готово",
              "failed": "не вышло", "cancelled": "снято"}


def run_files(graph: dict, base: Path, named: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Входные файлы прогона (имя в графе → base64): названные явно (`--file
    имя=путь`) и те, что узлы ядра грузят по имени и что лежат рядом с графом."""
    import base64

    paths = {name: Path(path) for name, path in (named or {}).items()}
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        for key in RUN_INPUTS.get(str(node.get("class_type")), ()):
            value = (node.get("inputs") or {}).get(key)
            if isinstance(value, str) and value not in paths and "/" not in value \
                    and (Path(base) / value).is_file():
                paths[value] = Path(base) / value
    out = {}
    for name, path in paths.items():
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EntryError(f"файл {path} не прочитать: {exc.strerror or exc}") from None
        if len(data) > MAX_RUN_FILE_BYTES:
            raise EntryError(f"файл {path.name} больше {MAX_RUN_FILE_BYTES // 2**20} МБ")
        out[name] = base64.b64encode(data).decode()
    return out


def run_state(doc: dict, graph: Optional[dict] = None) -> str:
    """Где прогон сейчас, одной строкой."""
    status = doc.get("status") or ""
    if status == "queued":
        if doc.get("stage") == "own_run":
            return "в очереди: ждёт, пока идёт ваш прошлый прогон"
        return "в очереди: реплика прогонов занята или поднимается"
    if status == "in_progress":
        if doc.get("phase"):
            return doc["phase"]
        at = doc.get("at") or {}
        if at.get("node"):
            kind = ((graph or {}).get(str(at["node"])) or {}).get("class_type")
            where = f"узел {at['node']}" + (f" ({kind})" if kind else "")
            step = f", шаг {at['value']} из {at['max']}" if at.get("max") else ""
            return f"{where}{step}"
        return "идёт" if doc.get("stage") != "weights" else "качаю веса"
    if status == "completed":
        took = (doc.get("timings") or {}).get("total_s")
        return f"готово{f' за {took:.0f} с' if isinstance(took, (int, float)) else ''}: " \
               f"файлов {len(doc.get('outputs') or [])}"
    if status == "failed":
        return f"не вышло: {(doc.get('error') or {}).get('message') or 'без причины'}"
    return RUN_STATES.get(status, status)


RUN_ID = re.compile(r"^run_[0-9a-f]{8,64}$")


def graph_source(looma, ref: str, write: str = "") -> Tuple[dict, Optional[Path]]:
    """Граф для разбора: файл или номер прогона (`run_…`, граф берётся с
    площадки). С `write` граф прогона ложится файлом рядом с черновиком
    описания (`run_….json`): описание ссылается на него. Ответ: граф и путь
    к файлу графа (у прогона без `write` — None)."""
    if RUN_ID.match(ref or "") and not Path(ref).exists():
        graph = looma.comfy.run_graph(ref)
        if not write:
            return graph, None
        target = Path(write).resolve().parent / f"{ref}.json"
        text = json.dumps(graph, ensure_ascii=False, indent=1) + "\n"
        if target.exists() and target.read_text(encoding="utf-8") != text:
            raise OpError(f"{target} уже есть и это другой граф: черновик не перезаписывает файл")
        target.write_text(text, encoding="utf-8")
        return graph, target
    return read_graph(ref), Path(ref)


def size_text(size) -> str:
    size = int(size or 0)
    return f"{size / 2 ** 30:.1f} ГиБ" if size >= 2 ** 30 else f"{max(1, size // 2 ** 20)} МБ"


def weights_lines(doc: dict) -> list:
    """Веса редактора строками (`looma comfy weights`)."""
    mine, shared = doc.get("mine") or [], doc.get("platform") or []
    lines = [f"свои веса: {len(mine)} из {doc.get('limit') or 100}"]
    if not mine:
        lines.append("  пока нет: `looma comfy weights add владелец/репозиторий/файл "
                     "--folder checkpoints`")
    for item in mine:
        lines.append(f"  {item['id']}  {item['folder']}  {item['name']}  {size_text(item.get('size'))}"
                     f"  коммит {str(item.get('revision') or '')[:8]}")
    lines.append("веса площадки (из открытых моделей ComfyUI): "
                 + (str(len(shared)) if shared else "пока нет"))
    for item in shared:
        lines.append(f"  {item['folder']}  {item['name']}  {size_text(item.get('size'))}"
                     f"  ({', '.join(item.get('models') or [])})")
    if mine or shared:
        lines.append("в графе файл называют так, как в списке (владелец/репозиторий/файл), или "
                     "только именем файла, если оно одно; прогон без --weights берёт их отсюда")
    return lines


def run_lines(doc: dict) -> list:
    """Прогон строками (`looma comfy runs <номер>`)."""
    lines = [f"{doc['id']}: {run_state(doc)}"]
    if doc.get("extensions"):
        lines.append(f"  наборы узлов: {', '.join(doc['extensions'])}")
    if doc.get("graph"):
        lines.append(f"  граф: {doc['graph']}")
    for item in doc.get("outputs") or []:
        where = "просмотр" if item.get("type") == "temp" else "результат"
        lines.append(f"  файл {item['index']}: узел {item.get('node')}, {item.get('filename')} "
                     f"({where}, {int(item.get('bytes') or 0) // 1024} КБ)")
    for node, values in (doc.get("ui") or {}).items():
        for key, value in values.items():
            lines.append(f"  узел {node}, {key}: {json.dumps(value, ensure_ascii=False)[:300]}")
    return lines


def save_run(looma, doc: dict, out: Path) -> list:
    """Файлы прогона в папку `out`: `<номер>_<узел>_<имя>`."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for item in doc.get("outputs") or []:
        name = Path(str(item.get("filename") or f"file{item['index']}")).name
        target = out / f"{item['index']}_{item.get('node')}_{name}"
        target.write_bytes(looma.comfy.output(doc["id"], item["index"]))
        saved.append(target)
    return saved


# --------------------------------------------------------------- движки
LAYER_KINDS = {"engine": "движок", "extension": "расширение"}
LAYER_STATES = {"pending": "ждёт проверки", "approved": "одобрена", "rejected": "отклонена",
                "draft": "черновик"}
IMAGE_STATES = {"pending": "образ собирается", "approved": "образ собран",
                "rejected": "образ не собран"}


def fee_text(fee: Optional[dict]) -> str:
    """Доля автора движка словами; пусто — доли нет."""
    fee = fee or {}
    parts = []
    if fee.get("price_call"):
        parts.append(f"{rubles(fee['price_call'])} за вызов")
    if fee.get("price_in") or fee.get("price_out"):
        parts.append(f"вход {rubles(fee.get('price_in') or 0)}, выход "
                     f"{rubles(fee.get('price_out') or 0)} за 1M токенов")
    return "; ".join(parts)


def fee_of(pairs) -> dict:
    """`call=0.2` (рубли за вызов) или `in=1 out=3` (рубли за 1M токенов) →
    доля автора движка в копейках."""
    fee = price_of(pairs)
    extra = sorted(set(fee) - {"price_in", "price_out", "price_call"})
    if extra:
        raise EntryError("доля автора движка считается за вызов (call) или за токены "
                         "(in и out)")
    return fee


def code_lines(code: dict) -> list:
    """Код одобренной версии движка — строками: откуда, команда, файлы."""
    source = code.get("source") or {}
    if source.get("commit"):
        where = f"{source.get('repo')} @ {str(source.get('commit'))[:12]}"
    else:
        where = f"папка автора {source.get('upload') or ''}".strip()
        if source.get("origin"):
            where += f", ссылка автора {source['origin']} (площадка её не проверяет)"
    lines = [f"{code['ref']}: версия {code['number']}, {LAYER_KINDS.get(code.get('kind'), '')}",
             f"  источник: {where}",
             f"  команда: {' '.join(code.get('command') or []) or '—'}",
             f"  зависимости: {', '.join(code.get('requirements') or []) or 'нет'}",
             "  файлы:"]
    lines += [f"    {f['name']} ({f['size']} Б)" for f in code.get("files") or []]
    lines.append(f"  файл целиком: looma engine code {code['ref']} --file <путь>")
    return lines


def layer_lines(layer: dict) -> list:
    """Движок или расширение автора с версиями — строками для человека."""
    head = f"{layer['ref']}: {LAYER_KINDS.get(layer['kind'], layer['kind'])}"
    if layer.get("title"):
        head += f", «{layer['title']}»"
    head += ", открыт авторам" if layer.get("public") else ", виден только вам"
    lines = [head]
    if layer.get("earned"):
        lines.append(f"  начислено с начала месяца: {rubles(layer['earned'])}")
    for version in layer.get("versions") or []:
        source = version.get("source") or {}
        # Движок-образ ревью не проходит: его версию одобряет сборка образа.
        states = IMAGE_STATES if version.get("form") == "image" else LAYER_STATES
        state = states.get(version["status"], version["status"])
        if version.get("review") == "own" and version["status"] == "approved":
            state = "без ревью, работает только на ваших узлах"
        origin = (f"коммит {str(source.get('commit') or '')[:12]}" if source.get("commit")
                  else f"из папки {source.get('upload') or ''}".strip())
        line = f"  версия {version['number']}: {state}, {origin}"
        if version.get("extends"):
            line += f", для движка {version['extends']}"
        if fee_text(version.get("fee")):
            line += f", доля: {fee_text(version.get('fee'))}"
        lines.append(line)
        if version.get("note"):
            lines.append(f"    {'сборка' if version.get('form') == 'image' else 'ревью'}: "
                         f"{version['note']}")
        for row in str(version.get("log") or "").splitlines()[-12:]:
            lines.append(f"      {row}")
    return lines


def model_price_text(model: dict) -> str:
    """Цена модели каталога строкой: за 1M токенов, изображение или секунду видео."""
    parts = []
    if model.get("price_in") is not None or model.get("price_out") is not None:
        parts.append(f"вход {rubles(model.get('price_in') or 0)}, выход "
                     f"{rubles(model.get('price_out') or 0)} за 1M токенов")
    if model.get("price_image") is not None:
        parts.append(f"{rubles(model['price_image'])} за изображение")
    if model.get("price_second") is not None:
        parts.append(f"{rubles(model['price_second'])} за секунду видео")
    return (" · " + "; ".join(parts)) if parts else ""


def node_lines(node: dict) -> list:
    """Свой узел строками: состояние, карта, режим, работа, движки."""
    title = node.get("title") or ""
    head = node["node_id"] + (f" «{title}»" if title and title != node["node_id"] else "")
    if node.get("pending"):
        state = "ждёт подключения: команду ещё не запускали"
    elif not node.get("online"):
        state = "не на связи"
    elif not node.get("accepts_tasks"):
        state = "на связи, задач не берёт" + (f": {node['refusal']}" if node.get("refusal") else "")
    else:
        state = "на связи"
    lines = [f"{head}: {state}"]
    if node.get("online"):
        card = node.get("gpu_name") or node.get("device") or "без видеокарты"
        lines.append(f"  {card}, карт {node.get('gpus_free', 0)} из {node.get('gpus_total', 0)} "
                     f"свободно, памяти свободно {node.get('vram_free_gib', 0)} ГиБ, агент "
                     f"{node.get('agent_version') or '?'}")
    lines.append("  " + ("делится с сетью" if node.get("shared") else "работает только на вас"))
    accepts = node.get("accepts") or {}
    if accepts.get("own") is False:
        lines.append("  свой код без ревью не принимает (кабинет, «Узлы» → «Настройки»)")
    for work in node.get("work") or []:
        lines.append(f"  работает: {work.get('label') or work.get('kind')}"
                     + ("" if work.get("mine") else " (сеть)"))
    for engine in node.get("engines") or []:
        if engine.get("mine") or engine.get("fits"):
            lines.append(f"  движок {engine['ref']} v{engine.get('version')}: "
                         + ("встанет" if engine.get("fits") else
                            f"не встанет: {engine.get('refusal')}"))
    if node.get("accrued"):
        lines.append(f"  начислено с начала месяца: {rubles(node['accrued'])}")
    return lines


def income_lines(doc: dict) -> list:
    """Доход автора за период строками: всё вместе, сервисы, модели, движки, узлы."""
    out = []
    overall = doc.get("overall") or {}
    if overall:
        out.append(f"всего начислено {rubles(overall.get('accrued', 0))}, комиссия "
                   f"{rubles(overall.get('fee', 0))}, себестоимость "
                   f"{rubles(overall.get('cost', 0))}, итог {rubles(overall.get('net', 0))}")
    for app in doc.get("services") or []:
        out.append(f"сервис {app.get('app')}: начислено {rubles(app.get('accrued', 0))}, "
                   f"себестоимость {rubles(app.get('cost', 0))}, итог "
                   f"{rubles(app.get('net', 0))}, клиентов {app.get('clients', 0)}")
    compute = doc.get("compute") or {}
    for model in compute.get("models") or []:
        out.append(f"модель {model.get('model')}: вызовов {model.get('calls', 0)}, начислено "
                   f"{rubles(model.get('accrued', 0))}, время карт "
                   f"{rubles(model.get('cost', 0))}, итог {rubles(model.get('net', 0))}")
    for engine in compute.get("engines") or []:
        out.append(f"движок {engine.get('ref')}: вызовов {engine.get('calls', 0)}, доля "
                   f"{rubles(engine.get('accrued', 0))}")
    for node in compute.get("nodes") or []:
        out.append(f"узел {node.get('node_id')}: {node.get('hours', 0)} ч карт, начислено "
                   f"{rubles(node.get('accrued', 0))}")
    if not out:
        out.append("за этот период начислений нет")
    out.append("выплат пока нет: это учёт")
    return out


def job_lines(doc: dict) -> list:
    """Задание строками: состояние, ход, результат."""
    line = f"{doc.get('id')}: {doc.get('status')}"
    if doc.get("progress") is not None and doc.get("status") in ("queued", "running"):
        line += f", {doc['progress']}%"
    out = [line]
    detail = doc.get("detail") or {}
    if detail.get("model"):
        out.append(f"  {detail.get('model')} {detail.get('method') or ''} "
                   f"{detail.get('path') or ''}".rstrip())
    error = doc.get("error")
    if error:
        out.append(f"  ошибка: {error.get('message') if isinstance(error, dict) else error}")
    result = doc.get("result") or {}
    if result:
        out.append(f"  ответ службы: {result.get('status')}, {result.get('bytes', 0)} байт, "
                   f"{result.get('content_type') or 'без типа'}")
    return out


def find_layer(looma: Looma, name: str) -> dict:
    doc = looma.engines.mine()
    wanted = {name, f"{doc.get('handle') or ''}/{name}"}
    found = next((layer for layer in doc["layers"] if layer["ref"] in wanted), None)
    if found is None:
        raise EntryError(f"движка или расширения {name!r} у вас нет: `looma engine list` "
                         "покажет свои")
    return found


def find_entry(looma: Looma, name: str) -> dict:
    entry = looma.entries.find(name)
    if entry is None:
        raise EntryError(f"модели {name!r} у вас нет: `looma model list` покажет свои")
    return entry


def add_entry(looma: Looma, payload: dict, handle: str = "") -> dict:
    """Завести запись. Имя автора задаётся один раз, перед первой записью."""
    known = looma.entries.mine().get("handle") or ""
    if handle and known and handle != known:
        raise EntryError(f"имя автора уже задано: {known}. Оно не меняется")
    if not known:
        if not handle:
            raise EntryError("сначала задайте имя автора, с него начинаются имена ваших "
                             "моделей: `looma model name <имя>`")
        looma.entries.set_handle(handle)
    return looma.entries.create(payload)


def stats_lines(doc: dict) -> list:
    """Статистика сервиса — строками для человека."""
    t = doc["totals"]
    channels = ", ".join(f"{CHANNEL_NAMES.get(k, k)} {v}" for k, v in doc["channels"].items() if v)
    lines = [f"{doc['app']} — за {doc['days']} дн.",
             f"  запросы: {t['requests']}" + (f" ({channels})" if channels else ""),
             f"  клиенты: всего {t['clients']}, новых {t['new_clients']}, "
             f"заходили {t['active_clients']}"]
    if doc["events"]:
        lines.append("  события: " + "; ".join(
            f"{e['title'] or e['name']} {e['quantity']} (клиентов: {e['clients']})"
            for e in doc["events"]))
    for m in doc["models"]:
        parts = [f"вызовы {m['calls']}"]
        if m["tokens_in"] or m["tokens_out"]:
            parts.append(f"токены {m['tokens_in']}/{m['tokens_out']}")
        if m["images"]:
            parts.append(f"изображения {m['images']}")
        if m["video_seconds"]:
            parts.append(f"видео {m['video_seconds']} с")
        lines.append(f"  {m['model']}: {', '.join(parts)}, {rubles(m['cost'])}")
    lines.append(f"  себестоимость: {rubles(t['cost'])} по текущему прайсу")
    if t.get("unpriced"):
        lines.append(f"  нет в прайсе (не посчитаны): {', '.join(t['unpriced'])}")
    if t.get("dev_model_calls"):
        lines.append(f"  looma dev: вызовы моделей {t['dev_model_calls']}")
    if doc["top_clients"]:
        lines.append("  самые активные: " + "; ".join(
            f"{c['client']} — событий {c['events']}, вызовов {c['model_calls']}"
            for c in doc["top_clients"][:5]))
    return lines


def listing_state(doc: dict) -> dict:
    """Карточка в маркетплейсе — коротко: что видно в каталоге, что ждёт,
    чем кончилась последняя модерация."""
    versions = doc.get("versions") or []
    decided = next((v for v in versions if v["status"] in ("approved", "rejected")), None)
    state = doc.get("state", "none")
    if state == "listed":
        # «listed» без одобренной версии — ещё не в каталоге.
        state = "live" if doc.get("live_version") else ("pending" if doc.get("pending")
                                                        else "rejected")
    out = {"app": doc.get("app"), "state": state, "in_catalog": state == "live",
           "live_version": doc.get("live_version"), "pending": doc.get("pending"),
           "url": doc.get("url"), "reason": doc.get("reason") or None}
    if decided:
        out["last_decision"] = {"version": decided["number"], "status": decided["status"],
                                "reason": decided.get("reason") or None}
    return out


def deploy(looma: Looma, root: Path, *, app: Optional[str] = None, promote: bool = False,
           wait: bool = True, on_progress: Optional[Callable[[str], None]] = None) -> dict:
    """Упаковать, собрать релиз, дождаться; не собралось — хвост лога сборки."""
    say = on_progress or (lambda text: None)
    root = Path(root).resolve()
    name = app or manifest_name(root)
    if not name:
        raise OpError(f"какой сервис? Нет имени в {root / MANIFEST} и не указано app")
    vendored = missing_sdk(root)
    if vendored:
        vendor(root)
        say("код импортирует loomafloat, а копии SDK в проекте не было: положил её в "
            "loomafloat/, как `looma sdk`")
    warnings = []
    try:
        checked = looma.apps.check_manifest((root / MANIFEST).read_text(encoding="utf-8"))
    except (LoomaError, OSError):
        checked = None              # старая платформа: ошибки покажет сборка
    if checked and not checked.get("ok"):
        raise OpError(f"{MANIFEST}: " + "; ".join(checked.get("problems") or []))
    for warning in (checked or {}).get("warnings") or []:
        warnings.append(warning)
        say(f"внимание: {warning}")
    archive, files, skipped = pack(root)
    say(f"{name}: {len(files)} файлов, {len(archive) / 1024:.0f} КБ")
    created = False
    if looma.apps.find(name) is None:
        looma.apps.create(name)
        created = True
        say(f"заведён сервис {name}")
    release = looma.apps.release(name, archive)
    number = release["number"]
    result = {"app": name, "release": number, "created": created, "files": len(files),
              "skipped_links": skipped, "vendored_sdk": vendored, "warnings": warnings,
              "status": "building"}
    if not wait:
        return result
    say(f"релиз {number} собирается…")
    started = time.monotonic()
    info = looma.apps.wait_release(name, number,
                                   on_poll=lambda _i: say(f"релиз {number}: сборка идёт"))
    result.update(status=info["status"], build_seconds=round(time.monotonic() - started))
    if info["status"] != "ready":
        result["error"] = info.get("error") or ""
        result["log_tail"] = "\n".join(looma.apps.release_log(name, number).splitlines()[-40:])
        return result
    urls = looma.apps.get(name).get("urls") or {}
    result["staging_url"] = urls.get("staging", "")
    if promote:
        looma.apps.promote(name, number)
        result["prod_url"] = urls.get("prod", "")
        result["promoted"] = True
    return result


def check(url: str, *, path: str = "/", timeout: float = 150.0) -> dict:
    """Запрос к адресу сервиса — проверка после записи. Спящий веб
    просыпается первым запросом, поэтому ждём щедро."""
    return _probe(url.rstrip("/") + "/" + path.lstrip("/"), timeout=timeout)


def check_app(looma: Looma, app: str, environment: str = "staging", path: str = "/",
              timeout: float = 150.0) -> dict:
    """Запрос к вебу сервиса через API владельца: доходит до процесса и при
    `access: clients`, где гость получил бы страницу входа. Будит спящий веб.
    Шлюз, сертификат и вход витрины так не проверить — для них `check` по
    публичному адресу."""
    query = "?looma_environment=staging" if environment == "staging" else ""
    target = looma.apps._url(app, "preview") + "/" + path.lstrip("/") + query
    result = _probe(target, timeout=timeout, headers={"Authorization": f"Bearer {looma.key}"})
    result.update(url=f"{environment}:{'/' + path.lstrip('/')}", via="owner")
    return result


def _probe(target: str, *, timeout: float, headers: Optional[dict] = None) -> dict:
    started = time.monotonic()
    request = urllib.request.Request(target, headers={"User-Agent": "loomafloat-check",
                                                      **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            status, body = answer.status, answer.read(2000)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read(2000) or b""
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        return {"url": target, "ok": False, "status": 0,
                "error": str(getattr(exc, "reason", exc)),
                "seconds": round(time.monotonic() - started, 1)}
    return {"url": target, "ok": 200 <= status < 400, "status": status,
            "seconds": round(time.monotonic() - started, 1),
            "body": body.decode(errors="replace")}


def app_url(looma: Looma, app: str, environment: str) -> str:
    url = (looma.apps.get(app).get("urls") or {}).get(environment) or ""
    if not url:
        raise OpError("адреса нет: у платформы не настроен домен сервисов")
    return url


__all__ = ["OpError", "SKILLS", "SKILL_DIRS", "app_url", "assess", "check", "check_app",
           "deploy", "init",
           "install_skills", "skills_root", "templates", "validate", "LoomaError"]
