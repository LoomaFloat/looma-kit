"""`looma`: сервис от исходников до прода из терминала.

    looma login                    ключ проекта (кабинет автора, раздел «Подключение»)
    looma init hello               заготовка сервиса
    looma dev                      запустить у себя: база и бакет в Docker, модели — dev-токеном
    looma deploy                   собрать релиз и поставить на staging
    looma status                   окружения, адреса, процессы
    looma logs [процесс] -f        лог процесса
    looma promote                  staging → prod
    looma rollback                 prod — на прошлый релиз
    looma secrets set KEY=VALUE    секреты процессов
    looma publish                  карточку и цены — в маркетплейс (через модерацию)
    looma stats                    запросы, клиенты, события, себестоимость

Сервис (app) берётся из `--app`, иначе по имени из `looma.yaml` текущей папки.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import socket
import sys
import time
import webbrowser
from pathlib import Path
from typing import List, Optional

from loomafloat import __version__, config, dev, ops
from loomafloat.client import Looma
from loomafloat.http import LoomaError
from loomafloat.pack import MANIFEST, PackError, manifest_name

ENVIRONMENTS = ("prod", "staging")


class Stop(Exception):
    """Остановиться с понятной причиной (код 1)."""


def say(text: str = "") -> None:
    print(text, flush=True)


def _looma(args) -> Looma:
    looma = Looma(api_url=getattr(args, "api", None))
    looma.key = config.user_key()
    if not looma.key:
        raise Stop("нет ключа: `looma login` (или переменная LOOMA_API_KEY)")
    return looma


def _app_name(args, root: Optional[Path] = None) -> str:
    if getattr(args, "app", None):
        return args.app
    root = root or Path.cwd()
    name = manifest_name(root)
    if name:
        return name
    raise Stop(f"какой сервис? Укажите --app или запустите в папке с {MANIFEST}")


def _when(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


# ------------------------------------------------------------- вход
def cmd_login(args) -> None:
    looma = Looma(api_url=args.api, key="")
    key = (args.key or "").strip()
    if not key and args.paste:
        say(f"Выпустите ключ в кабинете автора, в разделе «Подключение» ({config.KEYS_PAGE}), "
            "и вставьте его сюда.")
        key = getpass.getpass("ключ: ").strip()
    if not key:
        key = _device_login(looma, open_browser=not args.no_browser)
    looma.key = key
    try:
        me = looma.whoami()
    except LoomaError as exc:
        raise Stop(f"ключ не подошёл: {exc.message}") from None
    path = config.save({"api_url": looma.base, "key": key})
    who = me.get("email") or ("администратор" if me.get("role") == "admin" else "ключ принят")
    say(f"Готово: {who}, проект {me.get('project_id') or '—'}. Настройки — {path}")


def _device_login(looma: Looma, *, open_browser: bool = True) -> str:
    """Вход через браузер: код здесь, подтверждение в кабинете автора, ключ — сюда."""
    from loomafloat.http import send_json

    codes = send_json("POST", looma.url("/api/device/code"),
                      {"client": f"looma {__version__}", "hostname": socket.gethostname()})
    page = codes.get("verification_uri_complete") or codes["verification_uri"]
    say(f"Откройте {page}")
    say(f"и подтвердите вход. Код: {codes['user_code']}")
    if open_browser:
        try:
            webbrowser.open(page)
        except Exception:
            pass
    deadline = time.monotonic() + float(codes.get("expires_in") or 600)
    interval = max(1.0, float(codes.get("interval") or 3))
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            answer = send_json("POST", looma.url("/api/device/token"),
                               {"device_code": codes["device_code"]})
        except LoomaError as exc:
            if exc.kind == "authorization_pending":
                continue
            # Сеть моргнула или сервер перезапускается — код ещё жив, ждём дальше.
            if exc.status == 0 or exc.status == 429 or exc.status >= 500:
                continue
            raise Stop(exc.message) from None
        return answer["key"]
    raise Stop("код истёк: запустите `looma login` ещё раз")


def cmd_logout(args) -> None:
    say("Ключ забыт." if config.forget() else "Ключ и так не сохранён.")


def cmd_whoami(args) -> None:
    me = _looma(args).whoami()
    say(f"{me.get('email') or me.get('role') or 'ключ'} · проект {me.get('project_id') or '—'} · "
        f"права: {', '.join(me.get('scopes') or []) or '—'} · {config.api_url(args.api)}")


# --------------------------------------------------------- заготовка
def cmd_init(args) -> None:
    target = Path(args.dir or args.name or args.template)
    try:
        made = ops.init(target, args.template, args.name)
    except ops.OpError as exc:
        raise Stop(str(exc)) from None
    say(f"Заготовка «{args.template}» — в {made['directory']}. Дальше:")
    say(f"  cd {os.path.relpath(made['directory'])}")
    say("  looma dev       # у себя, с окружением платформы")
    say("  looma deploy    # релиз на staging")


def cmd_sdk(args) -> None:
    root = Path(args.dir)
    if not (root / MANIFEST).exists():
        raise Stop(f"в {root.resolve()} нет {MANIFEST}")
    files = ops.vendor(root)
    say(f"SDK loomafloat {__version__} — в {(root / 'loomafloat').resolve()} ({len(files)} файла)")


# ------------------------------------------------- оценка, проверка, skills
def cmd_assess(args) -> None:
    try:
        report = ops.assess(Path(args.dir))
    except ops.OpError as exc:
        raise Stop(str(exc)) from None
    text = report.pop("markdown")
    if args.json:
        say(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(text)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"отчёт — в {args.out}", file=sys.stderr if args.json else sys.stdout)


def cmd_validate(args) -> None:
    looma = Looma(api_url=getattr(args, "api", None))
    looma.key = config.user_key()
    try:
        result = ops.validate(looma if looma.key else None, Path(args.dir))
    except (ops.OpError, PackError) as exc:
        raise Stop(str(exc)) from None
    for problem in result["problems"]:
        say(f"ошибка: {problem}")
    for warning in result["warnings"]:
        say(f"внимание: {warning}")
    for model in result["models"]:
        state = {"ready": "отвечает", "cold": "спит", "missing": "нет в каталоге",
                 "unknown": "не проверена"}.get(model.get("status"), model.get("status"))
        say(f"модель {model['model']} (gpu.{model['step']}): {state}")
    if result["archive"]:
        say(f"архив: {result['archive']['files']} файлов, "
            f"{result['archive']['bytes'] / 1024:.0f} КБ")
    if not result["ok"]:
        raise Stop("манифест не годится: поправьте ошибки выше")
    say("манифест годится" + (": есть предупреждения" if result["warnings"] else ""))


def cmd_check(args) -> None:
    looma = _looma(args)
    app = _app_name(args)
    if args.public:
        result = ops.check(ops.app_url(looma, app, args.env), path=args.path)
    else:
        result = ops.check_app(looma, app, args.env, args.path)
    say(f"{result['url']}: " + (f"{result['status']} за {result['seconds']} с" if result["status"]
                                else f"нет ответа ({result.get('error')})"))
    if result.get("body"):
        say(result["body"][:600])
    if not result["ok"]:
        raise Stop("сервис не ответил как надо: `looma logs --env "
                   f"{args.env}` покажет почему")


def cmd_models(args) -> None:
    looma = _looma(args)
    rows = looma.models()
    if not rows:
        say("моделей нет")
        return
    width = max(len(m["id"]) for m in rows)
    for m in sorted(rows, key=lambda m: (m.get("kind") or "", m["id"])):
        state = "спит" if m.get("status") == "cold" else "отвечает"
        context = f" · контекст {m['context']}" if m.get("context") else ""
        vector = f" · вектор {m['dimensions']}" if m.get("dimensions") else ""
        say(f"{m['id']:<{width}}  {m.get('kind') or 'text':<9} {state}{context}{vector}")
    say("в манифест — по имени: gpu: { шаг: { model: <имя> } }")


def cmd_skill(args) -> None:
    if args.action == "path":
        say(str(ops.skills_root()))
        return
    agents = args.agent or ["claude"]
    project = Path(args.project) if args.project else None
    for agent in agents:
        try:
            placed = ops.install_skills(agent, project)
        except ops.OpError as exc:
            raise Stop(str(exc)) from None
        for path in placed:
            say(f"{agent}: {path}")
    say("агент подхватит skills в новой сессии")


# ------------------------------------------------------------- деплой
def cmd_deploy(args) -> None:
    looma = _looma(args)
    root = Path(args.dir or ".")
    try:
        result = ops.deploy(looma, root, app=args.app, promote=args.promote,
                            wait=not args.no_wait, on_progress=_progress())
    except (ops.OpError, PackError) as exc:
        raise Stop(str(exc)) from None
    number = result["release"]
    if result["skipped_links"]:
        say(f"ссылки не вошли в архив: {', '.join(result['skipped_links'][:5])}")
    if args.no_wait:
        say(f"релиз {number} собирается: `looma releases` покажет, чем кончилось")
        return
    if result["status"] != "ready":
        say(f"релиз {number} не собрался за {result['build_seconds']} с: {result.get('error')}")
        say("--- хвост лога сборки:")
        say("\n".join(result.get("log_tail", "").splitlines()[-30:]))
        raise Stop("сборка не удалась")
    say(f"релиз {number} собран за {result['build_seconds']} с и стоит на staging: "
        f"{result.get('staging_url') or '—'}")
    if result.get("promoted"):
        say(f"и переведён на prod: {result.get('prod_url') or '—'}")
    else:
        say("на prod — `looma promote`")


def _stop_on_signals() -> dict:
    """Закрыли терминал или прислали SIGTERM — остановиться, как по Ctrl+C:
    процессы сервиса не должны остаться без хозяина. Вернуть — что было."""
    def stop(_signum, _frame):
        raise KeyboardInterrupt
    before = {}
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            before[name] = signal.signal(getattr(signal, name), stop)
    return before


def cmd_dev(args) -> None:
    looma = _looma(args)
    before = _stop_on_signals()
    try:
        _dev(looma, args)
    finally:
        for name, handler in before.items():
            signal.signal(getattr(signal, name), handler)


def _dev(looma: Looma, args) -> None:
    root = Path(args.dir)
    try:
        if args.action == "down":
            dev.down(looma, root, wipe=args.wipe, say=say)
            return
        if args.action == "env":
            # Пояснения — в stderr: `eval "$(looma dev env)"` получает только export.
            for line in dev.env_lines(looma, root, args.process, with_data=not args.no_data,
                                      say=lambda text: print(text, file=sys.stderr, flush=True)):
                print(line)
            return
        if args.action == "run":
            if not args.process:
                raise Stop("какой процесс? `looma dev run <процесс>`")
            code = dev.run_once(looma, root, args.process, docker_mode=args.docker,
                                with_data=not args.no_data, say=say)
        else:
            only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
            code = dev.up(looma, root, docker_mode=args.docker, with_data=not args.no_data,
                          only=only, say=say)
    except (dev.DevError, PackError) as exc:
        raise Stop(str(exc)) from None
    if code:
        raise Stop(f"процесс завершился с кодом {code}")


def _progress():
    """Ход деплоя — строками, но без повторов «сборка идёт»."""
    last = {"text": ""}

    def show(text: str) -> None:
        if text != last["text"] and "сборка идёт" not in text:
            say(text)
        last["text"] = text
    return show


def cmd_releases(args) -> None:
    name = _app_name(args)
    for r in _looma(args).apps.releases(name):
        say(f"{r['number']:>4}  {r['status']:<9} {_when(r.get('created_at'))}  "
            f"{(r.get('error') or '')[:90]}")


# ------------------------------------------------------- состояние
def cmd_status(args) -> None:
    name = _app_name(args)
    app = _looma(args).apps.get(name)
    tls = app.get("tls") or {}
    say(f"{app['name']} ({app['id']}) · адрес {app.get('slug')} · сертификат: {tls.get('state', '—')}"
        + (f" — {tls.get('error')}" if tls.get("error") else ""))
    for env in app.get("environments") or []:
        say(f"\n{env['environment']}: релиз {env.get('release') or '—'} · {env['state']} · "
            f"{env.get('url') or ''}")
        for p in env.get("processes") or []:
            extra = []
            if p.get("restarts"):
                extra.append(f"падений {p['restarts']}")
            if p.get("next_run_at"):
                extra.append(f"следующий запуск {_when(p['next_run_at'])}")
            if p.get("last_exit"):
                extra.append(p["last_exit"][:70])
            say(f"  {p['process']}#{p['instance']} {p['kind']:<6} {p['state']:<9} "
                + " · ".join(extra))


def cmd_apps(args) -> None:
    rows = _looma(args).apps.list()
    if not rows:
        say("сервисов нет: `looma init`, потом `looma deploy`")
    for a in rows:
        say(f"{a['name']:<24} staging {a.get('staging_release') or '—':<4} "
            f"prod {a.get('prod_release') or '—':<4} {(a.get('urls') or {}).get('prod') or ''}")


def _default_process(looma: Looma, name: str, environment: str) -> str:
    rows = looma.apps.processes(name, environment)
    names = [p["process"] for p in rows]
    if not names:
        raise Stop(f"на {environment} у {name} нет запущенных процессов "
                   "(веб мог заснуть — откройте адрес, он проснётся)")
    return "web" if "web" in names else names[0]


def cmd_logs(args) -> None:
    name = _app_name(args)
    looma = _looma(args)
    process = args.process or _default_process(looma, name, args.env)
    seen = ""
    while True:
        text = looma.apps.logs(name, process, environment=args.env, instance=args.instance,
                               tail=args.tail)
        if not seen:
            sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
        elif text != seen:
            # Новое — то, что после уже показанного хвоста.
            overlap = seen[-2000:]
            index = text.rfind(overlap) if overlap else -1
            fresh = text[index + len(overlap):] if index >= 0 else text
            if fresh:
                sys.stdout.write(fresh if fresh.endswith("\n") else fresh + "\n")
        sys.stdout.flush()
        seen = text
        if not args.follow:
            return
        time.sleep(3)


# --------------------------------------------------------- окружения
def cmd_promote(args) -> None:
    name = _app_name(args)
    app = _looma(args).apps.promote(name, args.release)
    say(f"prod — релиз {app['prod_release']}: {(app.get('urls') or {}).get('prod') or ''}")


def cmd_rollback(args) -> None:
    name = _app_name(args)
    app = _looma(args).apps.rollback(name, args.env)
    field = "prod_release" if args.env == "prod" else "staging_release"
    say(f"{args.env} — снова релиз {app[field]}")


def cmd_stop(args) -> None:
    name = _app_name(args)
    _looma(args).apps.stop(name, args.env)
    say(f"{name}: {args.env or 'оба окружения'} остановлены; запустить — "
        "`looma promote` или `looma deploy`")


def cmd_restart(args) -> None:
    name = _app_name(args)
    try:
        rows = _looma(args).apps.restart(name, args.process, args.env)
    except LoomaError as exc:
        if exc.status == 404 and args.process == "web":
            say(f"web на {args.env} спит — проснётся первым запросом, уже с новыми секретами")
            return
        raise
    for p in rows:
        if p["process"] == args.process:
            say(f"{p['process']}#{p['instance']}: {p['state']}")


def cmd_open(args) -> None:
    name = _app_name(args)
    url = (_looma(args).apps.get(name).get("urls") or {}).get(args.env)
    if not url:
        raise Stop("адреса нет: у платформы не настроен домен сервисов")
    say(url)
    if not args.print_only:
        webbrowser.open(url)


def cmd_secrets(args) -> None:
    name = _app_name(args)
    looma = _looma(args)
    if args.action == "set":
        values = {}
        for pair in args.items:
            key, sep, value = pair.partition("=")
            if not sep:
                raise Stop(f"{pair!r}: нужно ИМЯ=значение")
            values[key] = value
        if not values:
            raise Stop("что поставить? looma secrets set ИМЯ=значение")
        rows = looma.apps.set_secrets(name, values)
        say(f"поставлено: {', '.join(values)}; процессы увидят их при перезапуске")
    elif args.action == "unset":
        if not args.items:
            raise Stop("что убрать? looma secrets unset ИМЯ")
        rows = looma.apps.set_secrets(name, {k: None for k in args.items})
        say(f"убрано: {', '.join(args.items)}")
    else:
        rows = looma.apps.secrets(name)
    for row in rows:
        say(f"  {row['name']:<32} {_when(row.get('updated_at'))}")


def _show_listing(doc: dict) -> None:
    state = ops.listing_state(doc)
    line = f"{state['app']}: {ops.STATES.get(state['state'], state['state'])}"
    if state["in_catalog"]:
        line += f", версия {state['live_version']}" + (f" — {state['url']}" if state["url"] else "")
    if state["reason"]:
        line += f" ({state['reason']})"
    say(line)
    if state["pending"]:
        say(f"  на модерации: версия {state['pending']}")
    last = state.get("last_decision")
    if last and last["status"] == "rejected":
        say(f"  версия {last['version']} — отказ: {last['reason']}")


def cmd_stats(args) -> None:
    looma = _looma(args)
    for line in ops.stats_lines(looma.apps.stats(_app_name(args), days=args.days)):
        say(line)


def cmd_publish(args) -> None:
    looma = _looma(args)
    name = _app_name(args)
    doc = looma.apps.publish(name)
    say(f"{name}: карточка и цены из релиза на prod отправлены на модерацию, "
        f"версия {doc['pending']}")
    say("ответ модератора покажет `looma listing`")


def cmd_listing(args) -> None:
    looma = _looma(args)
    _show_listing(looma.apps.listing(_app_name(args)))


def cmd_unpublish(args) -> None:
    looma = _looma(args)
    _show_listing(looma.apps.unpublish(_app_name(args)))
    say("вернуть — `looma publish` (снова через модерацию)")


def cmd_destroy(args) -> None:
    name = _app_name(args)
    if not args.yes:
        raise Stop(f"удаление {name} необратимо — база, бакет и релизы уйдут вместе с ним; "
                   "повторите с --yes")
    _looma(args).apps.delete(name)
    say(f"сервис {name} удалён")


# ---------------------------------------------------------------- MCP
MCP_HELP = """Claude Code:
  claude mcp add --transport stdio --scope user looma -- looma mcp
Codex:
  codex mcp add looma -- looma mcp
GitHub Copilot CLI:
  copilot mcp add looma -- looma mcp
Gemini CLI:
  gemini mcp add -s user looma looma mcp
Cursor (~/.cursor/mcp.json), Claude Code (.mcp.json проекта):
  {"mcpServers": {"looma": {"command": "looma", "args": ["mcp"]}}}
VS Code (.vscode/mcp.json):
  {"servers": {"looma": {"type": "stdio", "command": "looma", "args": ["mcp"]}}}
Ключ — тот же, что у CLI: сначала `looma login` (или LOOMA_API_KEY в env сервера).
Skills для агента: `looma skill install --agent <claude|cursor|codex|copilot|gemini|agents>`."""


def cmd_mcp(args) -> None:
    if args.print_config:
        say(MCP_HELP)
        return
    if args.install_skill:
        for path in ops.install_skills("claude"):
            say(f"skill — в {path}")
        say("Claude Code подхватит skills в новой сессии")
        return
    from loomafloat import mcp

    mcp.serve()


# -------------------------------------------------------------- разбор
def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="looma", description="Сервисы на Looma Float из терминала")
    top.add_argument("--version", action="version", version=f"looma {__version__}")
    top.add_argument("--api", help="адрес API (по умолчанию из `looma login`)")
    sub = top.add_subparsers(dest="command", required=True)

    def command(name, handler, help_text, *, app=False):
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.set_defaults(handler=handler)
        if app:
            p.add_argument("--app", help="сервис (по умолчанию — из looma.yaml)")
        return p

    p = command("login", cmd_login, "войти: через браузер или ключом проекта")
    p.add_argument("--key", help="ключ проекта (для CI; без него — вход через браузер)")
    p.add_argument("--paste", action="store_true", help="вставить ключ вручную")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер самим")
    command("logout", cmd_logout, "забыть ключ")
    command("whoami", cmd_whoami, "кто я и в каком проекте")

    p = command("init", cmd_init, "заготовка сервиса из шаблона")
    p.add_argument("dir", nargs="?", help="папка (по умолчанию — имя шаблона)")
    p.add_argument("--template", "-t", default="hello",
                   help="шаблон: hello, assistant, image-studio, video-studio")
    p.add_argument("--name", help="имя сервиса (по умолчанию — имя папки)")

    p = command("sdk", cmd_sdk, "положить в проект свежую копию SDK (папка loomafloat/)")
    p.add_argument("dir", nargs="?", default=".", help="папка с looma.yaml (по умолчанию — текущая)")

    p = command("dev", cmd_dev, "запустить у себя: база и бакет в Docker, модели — dev-токеном")
    p.add_argument("action", nargs="?", choices=("up", "run", "env", "down"), default="up",
                   help="up — запустить (по умолчанию), run — процесс один раз, "
                        "env — переменные, down — остановить базу и бакет")
    p.add_argument("process", nargs="?", help="процесс для run и env")
    p.add_argument("--dir", default=".", help="папка с looma.yaml (по умолчанию — текущая)")
    p.add_argument("--docker", action="store_true",
                   help="процессы — в образе, собранном как на платформе")
    p.add_argument("--no-data", action="store_true", help="без базы и бакета (без Docker)")
    p.add_argument("--only", help="какие процессы запустить, через запятую")
    p.add_argument("--wipe", action="store_true", help="down: удалить базу и бакет с данными")

    p = command("assess", cmd_assess, "оценить проект: подойдёт ли площадке и что переделать")
    p.add_argument("dir", nargs="?", default=".", help="папка проекта (по умолчанию — текущая)")
    p.add_argument("--json", action="store_true", help="отчёт в JSON")
    p.add_argument("--out", help="сохранить отчёт в файл (Markdown)")

    p = command("validate", cmd_validate, "проверить looma.yaml и проект без релиза")
    p.add_argument("dir", nargs="?", default=".", help="папка с looma.yaml (по умолчанию — текущая)")

    command("models", cmd_models, "модели каталога: имя, тип, отвечает или спит")

    p = command("check", cmd_check, "запрос к вебу сервиса: код, время, начало ответа",
                app=True)
    p.add_argument("--env", choices=ENVIRONMENTS, default="staging")
    p.add_argument("--path", default="/", help="путь на сайте сервиса")
    p.add_argument("--public", action="store_true",
                   help="по публичному адресу, как гость (иначе — через API владельца)")

    p = command("skill", cmd_skill, "skills Looma Float для ИИ-агентов")
    p.add_argument("action", nargs="?", choices=("install", "path"), default="install",
                   help="install — поставить (по умолчанию), path — где лежат")
    p.add_argument("--agent", action="append", choices=tuple(ops.SKILL_DIRS),
                   help="для какого агента (можно несколько раз; по умолчанию claude)")
    p.add_argument("--project", nargs="?", const=".",
                   help="в проект (папку) вместо домашнего каталога")

    p = command("deploy", cmd_deploy, "собрать релиз и поставить на staging", app=True)
    p.add_argument("dir", nargs="?", help="папка с looma.yaml (по умолчанию — текущая)")
    p.add_argument("--promote", action="store_true", help="и сразу на prod")
    p.add_argument("--no-wait", action="store_true", help="не ждать сборки")

    command("releases", cmd_releases, "релизы сервиса", app=True)
    command("status", cmd_status, "окружения, адреса, процессы", app=True)
    command("apps", cmd_apps, "мои сервисы")

    p = command("logs", cmd_logs, "лог процесса", app=True)
    p.add_argument("process", nargs="?", help="процесс (по умолчанию web)")
    p.add_argument("--env", choices=ENVIRONMENTS, default="prod")
    p.add_argument("--instance", type=int, default=0)
    p.add_argument("--tail", type=int, default=200)
    p.add_argument("-f", "--follow", action="store_true", help="следить")

    p = command("promote", cmd_promote, "релиз staging (или --release) — на prod", app=True)
    p.add_argument("--release", type=int)
    p = command("rollback", cmd_rollback, "окружение — на прошлый релиз", app=True)
    p.add_argument("--env", choices=ENVIRONMENTS, default="prod")
    p = command("stop", cmd_stop, "остановить окружение (или оба)", app=True)
    p.add_argument("--env", choices=ENVIRONMENTS)
    p = command("restart", cmd_restart, "перезапустить процесс; cron — запустить сейчас",
                app=True)
    p.add_argument("process")
    p.add_argument("--env", choices=ENVIRONMENTS, default="prod")
    p = command("open", cmd_open, "открыть адрес сервиса", app=True)
    p.add_argument("--env", choices=ENVIRONMENTS, default="prod")
    p.add_argument("--print-only", action="store_true", help="только напечатать адрес")

    p = command("secrets", cmd_secrets, "секреты процессов", app=True)
    p.add_argument("action", nargs="?", choices=("list", "set", "unset"), default="list")
    p.add_argument("items", nargs="*", help="ИМЯ=значение для set, ИМЯ для unset")

    p = command("stats", cmd_stats, "статистика: запросы, клиенты, события, модели, себестоимость",
                app=True)
    p.add_argument("--days", type=int, default=30, help="за сколько дней (до 90)")
    command("publish", cmd_publish,
            "отправить в маркетплейс карточку и цены из релиза на prod (через модерацию)",
            app=True)
    command("listing", cmd_listing, "карточка в маркетплейсе: в каталоге, на модерации, отказ",
            app=True)
    command("unpublish", cmd_unpublish, "снять карточку из каталога", app=True)

    p = command("mcp", cmd_mcp, "MCP-сервер для Claude Code, Cursor и других агентов (stdio)")
    p.add_argument("--print-config", action="store_true", help="как подключить к агенту")
    p.add_argument("--install-skill", action="store_true",
                   help="поставить skills для Claude Code (то же, что `looma skill install`)")

    p = command("destroy", cmd_destroy, "удалить сервис со всеми данными", app=True)
    p.add_argument("--yes", action="store_true")
    return top


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        args.handler(args)
    except Stop as exc:
        print(f"looma: {exc}", file=sys.stderr)
        return 1
    except LoomaError as exc:
        print(f"looma: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
