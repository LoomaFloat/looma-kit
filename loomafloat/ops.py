"""Действия над приложениями, общие для CLI и MCP: результат — данными, а
не печатью, чтобы одно и то же показывать человеку и отдавать агенту."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from loomafloat.client import Looma
from loomafloat.http import LoomaError
from loomafloat.pack import MANIFEST, PackError, ignored, manifest_name, pack, patterns

TEMPLATES = Path(__file__).with_name("templates")
#: Шаблону нужен SDK — копия ложится в проект: приложение собирается на
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
#: Skills для агентов: перенос проекта на площадку и работа с приложением.
SKILLS = ("looma-port", "looma-apps")

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
            if exc.status == 404:
                warnings.append("платформа пока не проверяет манифест без релиза: "
                                "ошибки покажет сборка")
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


def stats_lines(doc: dict) -> list:
    """Статистика приложения — строками для человека."""
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
            parts.append(f"картинки {m['images']}")
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
        raise OpError(f"какое приложение? Нет имени в {root / MANIFEST} и не указано app")
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
        say(f"заведено приложение {name}")
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
    """Запрос к адресу приложения — проверка после записи. Спящий веб
    просыпается первым запросом, поэтому ждём щедро."""
    return _probe(url.rstrip("/") + "/" + path.lstrip("/"), timeout=timeout)


def check_app(looma: Looma, app: str, environment: str = "staging", path: str = "/",
              timeout: float = 150.0) -> dict:
    """Запрос к вебу приложения через API владельца: доходит до процесса и при
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
        raise OpError("адреса нет: у платформы не настроен домен приложений")
    return url


__all__ = ["OpError", "SKILLS", "SKILL_DIRS", "app_url", "assess", "check", "check_app",
           "deploy", "init",
           "install_skills", "skills_root", "templates", "validate", "LoomaError"]
