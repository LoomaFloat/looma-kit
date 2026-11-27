"""Куратор рецептов: учебный сервис входа через Looma с правами.

    GET  /        страница: без согласия кнопка «Разрешить доступ», с ним форма рецепта
    POST /save    форма → черновик рецепта в каталоге человека
    GET  /health  жив ли процесс

Сервис объявляет в looma.yaml право `catalog.draft` (раздел `account`). Когда
человек разрешил сервису действовать в своём каталоге, шлюз площадки кладёт в
каждый его запрос ключ человека: заголовок `X-Looma-Account-Key`, на десять
минут. Сервис зовёт API площадки своим токеном вместе с этим ключом:
`looma.for_person(ключ)`. Запись ложится в каталог человека с пометкой
«сделано сервисом».

Ключа нет — человек ещё не разрешил (или отозвал доступ в кабинете): ведём
его на `/.looma/consent`. Этот адрес держит шлюз площадки, а не сервис. У себя
(`looma dev`) шлюза нет и ключа тоже: проверяйте сервис на staging.
"""

import html
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from loomafloat import Looma, LoomaError, consent_url, person_of

#: Модель каталога, поверх которой рецепт: она должна быть в каталоге площадки.
BASE = os.environ.get("CURATOR_BASE", "Qwen3.8-Flash-Next-NVFP4")
CONSOLE = os.environ.get("CURATOR_CONSOLE", "https://console.loomafloat.ru").rstrip("/")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")
looma = Looma()

STYLE = """
 body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f7f7;color:#152022}
 main{max-width:640px;margin:0 auto;padding:24px 16px}
 h1{font-size:28px;line-height:1.2;margin:0 0 8px}
 p{margin:0 0 16px}.muted{color:#5b6b6e;font-size:14px}
 .card{background:#fff;border:1px solid #dde3e4;border-radius:12px;padding:16px;margin:16px 0}
 label{display:block;font-weight:600;margin:12px 0 4px}
 input,textarea{width:100%;box-sizing:border-box;font:inherit;padding:10px;border-radius:8px;border:1px solid #c6cfd1}
 textarea{min-height:120px;resize:vertical}
 .btn{display:inline-block;font:inherit;padding:10px 18px;border-radius:10px;border:0;background:#0b7f91;
      color:#fff;text-decoration:none;cursor:pointer;margin-top:16px}
 .ok{border-color:#9fd3b4;background:#f1faf4}.bad{border-color:#efb4ad;background:#fdf3f2}
 ul{padding-left:20px;margin:0}code{font-size:14px}
"""


def page(body: str) -> bytes:
    return (f'<!doctype html><html lang="ru"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Куратор рецептов · Looma Float</title><style>{STYLE}</style>"
            f"<main><h1>Куратор рецептов</h1>{body}</main></html>").encode()


def consent_block() -> str:
    """Человек ещё не разрешил: объяснить и отправить за согласием."""
    return ("<p>Сервис заводит черновик рецепта в вашем каталоге Looma Float: настройку "
            "поверх модели. Из черновика вы потом сделаете свою модель в кабинете автора.</p>"
            '<div class="card"><p>Для этого нужно ваше разрешение. Сервис получит право '
            "заводить черновики в вашем каталоге и ничего больше: денег, ключей и почты он "
            "не касается. Отозвать доступ можно в кабинете, раздел «Доступы сервисов».</p>"
            f'<a class="btn" href="{html.escape(consent_url("/"))}">Разрешить доступ</a></div>')


def form_block(mine: list) -> str:
    made = "".join(f"<li><code>{html.escape(r['ref'])}</code>, версия {r.get('version')}</li>"
                   for r in mine) or "<li>пока ничего</li>"
    return ('<form class="card" method="post" action="save">'
            '<label for="name">Имя рецепта</label>'
            '<input id="name" name="name" required pattern="[a-z0-9][a-z0-9._-]{1,62}" '
            'placeholder="contracts">'
            '<p class="muted">Латиница в нижнем регистре, цифры, точка, дефис.</p>'
            '<label for="system">Чем модель занята</label>'
            '<textarea id="system" name="system" required placeholder="Ты помогаешь юристу '
            'проверять договоры поставки…"></textarea>'
            '<label for="summary">Описание в одну строку</label>'
            '<input id="summary" name="summary" maxlength="160">'
            f'<p class="muted">Рецепт работает поверх модели <code>{html.escape(BASE)}</code>.</p>'
            '<button class="btn" type="submit">Завести черновик</button></form>'
            f'<div class="card"><b>Заведено здесь</b><ul>{made}</ul></div>')


def made_here(person: str) -> list:
    """Рецепты человека, которые завёл этот сервис (пометка made_by)."""
    view = looma.for_person(person).recipes.view()
    me = os.environ.get("LOOMA_APP_ID", "")
    return [r for r in view.get("mine") or [] if (r.get("made_by") or {}).get("app") == me]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if self.path.split("?")[0] != "/":
            return self._send(404, page("<p>Такой страницы нет.</p>"))
        person = person_of(self.headers)
        if not person:
            return self._send(200, page(consent_block()))
        try:
            mine = made_here(person)
        except LoomaError as exc:
            if exc.kind == "consent_required":  # отозвали доступ или ключ истёк
                return self._send(200, page(consent_block()))
            return self._send(502, page(f'<div class="card bad">{html.escape(exc.message)}</div>'))
        return self._send(200, page(form_block(mine)))

    def do_POST(self):
        if self.path != "/save":
            return self._send(404, page("<p>Такой страницы нет.</p>"))
        person = person_of(self.headers)
        if not person:
            return self._send(200, page(consent_block()))
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        form = {k: v[-1] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
        name = form.get("name", "").strip().lower()
        system = form.get("system", "").strip()
        if not NAME.match(name) or not system:
            return self._send(400, page('<div class="card bad">Нужны имя рецепта (латиница, '
                                        'цифры, точка, дефис) и описание задачи.</div>'
                                        '<a class="btn" href="/">Назад</a>'))
        try:
            # Рецепт с тем же именем получил бы новую версию: чужой (не этого
            # сервиса) рецепт человека не трогаем.
            taken = {r["name"]: r for r in looma.for_person(person).recipes.view().get("mine") or []}
            if name in taken and (taken[name].get("made_by") or {}).get("app") != \
                    os.environ.get("LOOMA_APP_ID", ""):
                return self._send(409, page(
                    f'<div class="card bad">Рецепт <code>{html.escape(taken[name]["ref"])}</code> '
                    "у вас уже есть, его завели не здесь. Возьмите другое имя.</div>"
                    '<a class="btn" href="/">Назад</a>'))
            made = looma.for_person(person).recipes.add(
                name, base=BASE, preset={"system": system[:4000]},
                summary=form.get("summary", "").strip()[:160])
        except LoomaError as exc:
            if exc.kind == "consent_required":  # отозвали доступ, пока человек писал
                return self._send(200, page(consent_block()))
            # Остальное: имя занято чужой записью, нет основы, путь закрыт правами
            # (`insufficient_rights`) — согласие тут не поможет, показать причину.
            return self._send(400 if exc.status < 500 else 502, page(
                f'<div class="card bad">Не получилось: {html.escape(exc.message)}</div>'
                '<a class="btn" href="/">Назад</a>'))
        ref = html.escape(made["ref"])
        return self._send(200, page(
            f'<div class="card ok">Черновик <code>{ref}</code> заведён, версия '
            f"{made.get('version')}. Он в кабинете автора, раздел «Рецепты»: оттуда из него "
            "делают свою модель.</div>"
            f'<a class="btn" href="{CONSOLE}/recipes/{ref}">Открыть в кабинете</a> '
            '<a class="btn" href="/">Ещё рецепт</a>'))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
