"""Telegram-бот на Looma Float: изображения, правка фото и видео по описанию.

Вебхук бот ставит сам, на платформу (`LOOMA_TELEGRAM_WEBHOOK_URL`, канал
`telegram` в looma.yaml): она сверяет секрет Telegram и приносит обновление на
`POST /telegram` с `X-Looma-Client` — псевдонимом отправителя. На него и
записываются вызовы моделей и события `picture` и `video`. Без канала бот
ставит вебхук на свой адрес и сверяет секрет сам.

Ответ Telegram — сразу, генерация — в фоне: ролик считается минуты, а
Telegram ждёт ответа на вебхук секунды и повторяет его, если не дождался.
"""

import hashlib
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loomafloat import Looma, LoomaError, client_of

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
IMAGE_MODEL = os.environ.get("BOT_IMAGE_MODEL", "Qwen-Image-2.1")
VIDEO_MODEL = os.environ.get("BOT_VIDEO_MODEL", "MiniMax-H3")
#: Где бот ставит вебхук: окружение платформы с этим именем (`LOOMA_ENVIRONMENT`).
WEBHOOK_ENV = os.environ.get("BOT_WEBHOOK_ENV", "prod")
#: Вебхук через платформу: адрес и секрет — от неё (канал telegram в looma.yaml).
PLATFORM_URL = os.environ.get("LOOMA_TELEGRAM_WEBHOOK_URL", "").strip()
#: Секрет вебхука: от платформы или из токена — одинаковый во всех процессах.
SECRET = (os.environ.get("LOOMA_TELEGRAM_SECRET", "").strip()
          or (hashlib.sha256(f"looma-webhook:{TOKEN}".encode()).hexdigest()[:48] if TOKEN else ""))
HELP = ("Пришлите описание, и я нарисую изображение.\n"
        "Фото с подписью — поправлю фото по подписи.\n"
        "/video описание — сниму ролик на 5 секунд (это несколько минут).")
#: Одновременно в работе — не больше стольких заданий; остальные ждут.
WORKERS = threading.BoundedSemaphore(int(os.environ.get("BOT_WORKERS", "4")))
looma = Looma()


# ------------------------------------------------------------ Telegram
def telegram(method, payload=None, files=None, timeout=120):
    """Вызов Bot API; файлы — multipart. Ответ — поле result."""
    url = f"{TELEGRAM}/bot{TOKEN}/{method}"
    if files:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in (payload or {}).items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                         f"{value}\r\n".encode())
        for key, (filename, data, kind) in files.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"; "
                         f"filename=\"{filename}\"\r\nContent-Type: {kind}\r\n\r\n".encode()
                         + data + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    else:
        body = json.dumps(payload or {}).encode()
        headers = {"Content-Type": "application/json"}
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return json.loads(answer.read()).get("result")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram {method}: {exc.code} {exc.read()[:200]!r}") from None


def say(chat, text, reply_to=None):
    payload = {"chat_id": chat, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    telegram("sendMessage", payload)


def download_photo(message):
    """Самый крупный вариант фото из сообщения — байтами."""
    file_id = message["photo"][-1]["file_id"]
    path = telegram("getFile", {"file_id": file_id})["file_path"]
    with urllib.request.urlopen(f"{TELEGRAM}/file/bot{TOKEN}/{path}", timeout=120) as answer:
        return answer.read()


# ------------------------------------------------------------ работа
def charge(client, event):
    """Событие для учёта по клиенту маркетплейса; без клиента — нечего."""
    if client:
        try:
            looma.charge(event, client)
        except LoomaError as exc:
            print(f"событие {event} не записалось: {exc.message}", flush=True)


def handle(message, client=None):
    ai = looma.for_client(client)     # вызовы моделей — на отправителя
    chat = message["chat"]["id"]
    reply_to = message.get("message_id")
    text = (message.get("text") or message.get("caption") or "").strip()
    try:
        if text in ("/start", "/help"):
            return say(chat, HELP)
        if text.startswith("/video"):
            prompt = text[len("/video"):].strip()
            if not prompt:
                return say(chat, "Опишите, что снять: /video рыжая лиса бежит по снегу")
            say(chat, "Снимаю ролик — это несколько минут.", reply_to)
            data = ai.video(VIDEO_MODEL, prompt, seconds=5, quality="fast").content()
            telegram("sendVideo", {"chat_id": chat, "reply_to_message_id": reply_to},
                     files={"video": ("video.mp4", data, "video/mp4")}, timeout=300)
            return charge(client, "video")
        if message.get("photo"):
            if not text:
                return say(chat, "Подпишите фото: что в нём поменять.", reply_to)
            telegram("sendChatAction", {"chat_id": chat, "action": "upload_photo"})
            picture = ai.image(IMAGE_MODEL, text, images=[download_photo(message)])[0]
        elif text:
            telegram("sendChatAction", {"chat_id": chat, "action": "upload_photo"})
            picture = ai.image(IMAGE_MODEL, text)[0]
        else:
            return say(chat, HELP)
        telegram("sendPhoto", {"chat_id": chat, "reply_to_message_id": reply_to},
                 files={"photo": ("image.png", picture, "image/png")})
        charge(client, "picture")
    except LoomaError as exc:
        say(chat, f"Не получилось: {exc.message}", reply_to)
    except Exception as exc:          # бот не должен молчать из-за чего угодно
        print(f"ошибка на сообщении {reply_to}: {exc!r}", flush=True)
        try:
            say(chat, "Что-то сломалось, попробуйте ещё раз.", reply_to)
        except Exception:
            pass


def in_background(message, client=None):
    def run():
        with WORKERS:
            handle(message, client)
    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    def _send(self, status, text="ok"):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200)
        if not TOKEN:
            return self._send(200, "Бот ждёт токена: looma secrets set TELEGRAM_BOT_TOKEN=… "
                                   "и перезапуск (looma restart web)")
        self._send(200, "Бот работает. Напишите ему в Telegram.")

    def do_POST(self):
        if not self.path.startswith("/telegram"):
            return self._send(404, "нет такого пути")
        # Через платформу: секрет сверила она, а X-Looma-Client-Via извне не
        # подделать — шлюз вырезает входящие X-Looma-*. Напрямую — свой секрет.
        via_platform = bool(PLATFORM_URL) and self.headers.get("X-Looma-Client-Via") == "telegram"
        if not TOKEN or not (via_platform
                             or self.headers.get("X-Telegram-Bot-Api-Secret-Token") == SECRET):
            return self._send(403, "чужой запрос")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            update = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, "не JSON")
        message = update.get("message") or update.get("edited_message")
        if message and message.get("chat"):
            in_background(message, client_of(self.headers))
        self._send(200)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


def set_webhook():
    """Вебхук — на платформу (или на свой адрес), и только в одном окружении
    (`BOT_WEBHOOK_ENV`)."""
    own = os.environ.get("LOOMA_APP_URL", "").strip()
    environment = os.environ.get("LOOMA_ENVIRONMENT", "local")
    # Платформа отдаёт обновления только prod: бот staging принимает их сам.
    url = (PLATFORM_URL if PLATFORM_URL and environment == "prod"
           else own.rstrip("/") + "/telegram" if own else "")
    if not TOKEN:
        print("нет TELEGRAM_BOT_TOKEN: looma secrets set TELEGRAM_BOT_TOKEN=…", flush=True)
        return
    if environment != WEBHOOK_ENV or not url:
        print(f"окружение {environment}: вебхук ставится только в {WEBHOOK_ENV}", flush=True)
        return
    try:
        telegram("setWebhook", {"url": url, "secret_token": SECRET,
                                "allowed_updates": ["message", "edited_message"],
                                "drop_pending_updates": False})
        print(f"вебхук: {url}", flush=True)
    except Exception as exc:
        print(f"вебхук не поставился: {exc}", flush=True)


if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get("PORT", "8080"))
    print(f"бот слушает 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
