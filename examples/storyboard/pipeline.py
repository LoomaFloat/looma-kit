"""Конвейер ролика: идея → сценарий → кадры → ролик. На каждом шаге своя модель.

    сценарий   Qwen3.8-Flash-Next-NVFP4  идея по-русски → план кадра и промпты по-английски
    кадры      Qwen-Image-2.1            первый кадр (варианты) и, по желанию, последний
    ролик      MiniMax-H3                ролик со звуком, который начинается выбранным кадром

Проект хранится JSON-файлом: `<клиент>/<проект>/project.json`, рядом кадры
(`start-….png`, `end-….png`) и дубли (`take-….mp4`). Сценарий и кадры
считаются в фоновом потоке процесса. Ролик считает платформа заданием, а
проект помнит только его номер. Поэтому сон процесса ролик не теряет:
следующий запрос спросит платформу и заберёт готовый дубль в хранилище.

У каждого клиента каталога (заголовок X-Looma-Client) своя папка,
чужие проекты не видны. Вызовы моделей идут на клиента
(`looma.for_client`), готовый дубль отмечается событием `video`.
"""

import base64
import hashlib
import json
import random
import re
import threading
import time
import traceback
import uuid

from loomafloat import Looma, LoomaError, Video
from loomafloat.http import send_json

from store import Store

#: Те же имена, что в разделе gpu в looma.yaml. Если модели нет в манифесте, платформа ответит 403.
SCRIPT_MODEL = "Qwen3.8-Flash-Next-NVFP4"
FRAME_MODEL = "Qwen-Image-2.1"
VIDEO_MODEL = "MiniMax-H3"

#: Формат кадра → размер. Стороны кратны 32, площадь не больше 1344×768 (предел MiniMax-H3);
#: кадры рисуются в том же размере, что и ролик.
FORMATS = {"16:9": "1344x768", "9:16": "768x1344", "1:1": "960x960"}
ASPECT_WORDS = {"16:9": "горизонтальный, 16:9", "9:16": "вертикальный, 9:16",
                "1:1": "квадратный, 1:1"}
#: draft: около 3 минут на 5 секунд, fast: около 6; standard считается полчаса.
QUALITIES = ("draft", "fast")
SECONDS = (5, 10)
START_VARIANTS = 3
END_VARIANTS = 2
MAX_FRAMES = 12
MAX_TAKES = 6
#: Как часто спрашивать платформу о дубле в работе.
POLL_S = 8
#: Шаг «в работе», которого нет среди потоков процесса, прервался перезапуском.
STALE_S = 20

SCRIPT_FIELDS = {"title": 80, "logline": 300, "mood": 60, "camera": 80, "sound": 160,
                 "frame_prompt": 1500, "video_prompt": 1800, "end_prompt": 800}
EDITABLE = ("title", "frame_prompt", "video_prompt", "end_prompt")

SCRIPT_SYSTEM = """Ты режиссёр коротких роликов. По идее пользователя спланируй один \
непрерывный план длиной {seconds} секунд для модели, которая делает видео со звуком. \
Кадр {aspect}.

Ответь только JSON-объектом, без пояснений и без markdown. Поля:
"title": название по-русски, 2–5 слов;
"logline": одно предложение по-русски о том, что происходит в кадре;
"mood": настроение по-русски, 1–3 слова;
"camera": движение камеры по-русски, 2–5 слов;
"sound": что слышно, по-русски, до 10 слов;
"frame_prompt": по-английски, 40–80 слов: первый кадр как фотография. Герой, место, \
композиция, свет, оптика, стиль. Без движения и без событий;
"video_prompt": по-английски, 50–90 слов: что движется и как, движение камеры, темп, \
атмосфера. В конце "Audio: ..." со звуками сцены;
"end_prompt": по-английски, до 40 слов: как изменить первый кадр, чтобы получился \
последний кадр этого же плана. Герои, место, свет и стиль остаются прежними.

В кадре не должно быть текста, надписей и логотипов."""

_ID = re.compile(r"^[0-9a-f]{16}$")
_MEDIA = re.compile(r"^((start|end)-[0-9a-f]{8}\.png|take-[0-9a-f]{8}\.mp4)$")

looma = Looma()
store = Store()

_locks = {}
_locks_guard = threading.Lock()
#: Шаги, которые сейчас считаются в потоках этого процесса: (проект, шаг).
_running = set()
#: Когда платформу последний раз спрашивали о дубле: номер дубля → time.monotonic().
_checked = {}


class Problem(Exception):
    """Ошибка, которую видит пользователь: код ответа и причина словами."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ------------------------------------------------------------ хранение
def folder_of(client):
    """Папка клиента: псевдоним берётся хешем, чтобы в ключах не было лишних знаков."""
    return "c/" + hashlib.sha256((client or "").encode()).hexdigest()[:20]


def _key(base, pid, name="project.json"):
    return f"{base}/{pid}/{name}"


def lock_of(pid):
    with _locks_guard:
        return _locks.setdefault(pid, threading.Lock())


def load(base, pid):
    if not _ID.match(pid or ""):
        raise Problem(404, "Такого проекта нет")
    try:
        return json.loads(store.get(_key(base, pid)))
    except KeyError:
        raise Problem(404, "Такого проекта нет") from None


def save(base, project):
    project["updated"] = int(time.time())
    store.put(_key(base, project["id"]), json.dumps(project, ensure_ascii=False).encode(),
              "application/json")


def update(base, pid, change):
    """Прочитать, поменять и записать проект под замком: два запроса подряд
    не затрут друг друга."""
    with lock_of(pid):
        project = load(base, pid)
        change(project)
        save(base, project)
        return project


def _now():
    return int(time.time())


def _reason(exc):
    """Причина словами для пользователя, подробности уходят в лог процесса."""
    if isinstance(exc, Problem):
        return exc.message
    if isinstance(exc, LoomaError):
        print(f"модель: {exc.status} {exc.message}", flush=True)
        if exc.status == 403:
            return "Модель недоступна сервису: её нет в разделе gpu looma.yaml"
        if exc.status == 429:
            return "Модель сейчас занята. Попробуйте через минуту"
        return f"Модель не ответила: {exc.message}"
    if isinstance(exc, ValueError):
        return "Модель ответила не по формату. Попробуйте ещё раз"
    traceback.print_exc()
    return "Что-то пошло не так. Попробуйте ещё раз"


def _begin(project, step):
    part = project[step]
    if part["status"] == "working":
        raise Problem(409, "Этот шаг уже идёт")
    part.update(status="working", error="", started=_now())


def _fail(step, exc):
    reason = _reason(exc)

    def change(project):
        project[step].update(status="failed", error=reason)
    return change


def _background(pid, step, work):
    _running.add((pid, step))

    def run():
        try:
            work()
        except Problem as exc:              # проект удалили, пока шаг считался
            print(f"проект {pid}, шаг {step}: {exc.message}", flush=True)
        finally:
            _running.discard((pid, step))
    threading.Thread(target=run, name=f"{step}-{pid}", daemon=True).start()


# ------------------------------------------------------------ проект
def create(client, idea, fmt, seconds, quality, auto=False):
    idea = " ".join(str(idea or "").split())
    if len(idea) < 3:
        raise Problem(400, "Опишите идею хотя бы парой слов")
    if fmt not in FORMATS:
        raise Problem(400, f"Формат: один из {', '.join(FORMATS)}")
    if seconds not in SECONDS:
        raise Problem(400, "Длительность: 5 или 10 секунд")
    if quality not in QUALITIES:
        raise Problem(400, "Качество: draft или fast")
    base = folder_of(client)
    pid = f"{int(time.time() * 1000):012x}{uuid.uuid4().hex[:4]}"
    project = {
        "id": pid, "client": client or "", "created": _now(), "idea": idea[:600],
        "format": fmt, "seconds": seconds, "quality": quality, "auto": bool(auto),
        "script": {"status": "idle", "error": "", **{k: "" for k in SCRIPT_FIELDS}},
        "frames": {"status": "idle", "error": "", "doing": "", "start": [], "end": [],
                   "chosen_start": "", "chosen_end": ""},
        "takes": [],
    }
    save(base, project)
    return write_script(client, pid)


def remove(client, pid):
    base = folder_of(client)
    project = load(base, pid)
    for take in project["takes"]:
        if take["status"] == "working":         # не считать ролик, который никто не увидит
            try:
                send_json("POST", looma.url(f"/v1/videos/{take['job']}/cancel"), {},
                          key=looma.key)
            except LoomaError as exc:
                print(f"дубль {take['id']} не отменился: {exc.message}", flush=True)
    store.delete_folder(f"{base}/{pid}/")


def projects(client, limit=40):
    base = folder_of(client)
    out = []
    for pid in sorted((p for p in store.folders(base + "/") if _ID.match(p)), reverse=True):
        try:
            project = fresh(client, pid)
        except Problem:
            continue
        out.append(summary(project))
        if len(out) >= limit:
            break
    return out


def fresh(client, pid):
    """Проект в текущем состоянии: о дублях в работе спросить платформу,
    шаг «в работе» без потока отметить прерванным."""
    base = folder_of(client)
    project = load(base, pid)
    stale = [step for step in ("script", "frames")
             if project[step]["status"] == "working" and (pid, step) not in _running
             and _now() - project[step].get("started", 0) > STALE_S]
    if stale:
        def change(p):
            for step in stale:
                if p[step]["status"] == "working" and (pid, step) not in _running:
                    p[step].update(status="failed",
                                   error="Шаг прервался: сервис перезапускался. Повторите")
        project = update(base, pid, change)
    return _settle(base, project)


# ------------------------------------------------------------ 1. сценарий
def write_script(client, pid, note=""):
    """Qwen пишет план кадра и промпты. С `note` переписывает прошлый вариант."""
    base = folder_of(client)
    note = " ".join(str(note or "").split())[:300]
    project = update(base, pid, lambda p: _begin(p, "script"))
    previous = project["script"] if project["script"].get("video_prompt") else None

    def work():
        try:
            script = _compose(client, project, previous, note)
        except Exception as exc:
            update(base, pid, _fail("script", exc))
            return

        def done(p):
            p["script"].update(script, status="ready", error="")
        done_project = update(base, pid, done)
        if done_project["auto"]:
            _auto(client, pid, "start")

    _background(pid, "script", work)
    return project


def _compose(client, project, previous, note):
    system = SCRIPT_SYSTEM.format(seconds=project["seconds"],
                                  aspect=ASPECT_WORDS[project["format"]])
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"Идея: {project['idea']}"}]
    if previous:
        messages.append({"role": "assistant", "content": json.dumps(
            {k: previous.get(k, "") for k in SCRIPT_FIELDS}, ensure_ascii=False)})
        messages.append({"role": "user", "content":
                         f"Перепиши план с учётом пожелания: {note}" if note
                         else "Предложи другой вариант этого же плана."})
    ai = looma.for_client(client or None)
    for attempt in range(2):            # модель иногда отвечает не JSON: тогда второй заход
        text = ai.chat(SCRIPT_MODEL, messages, temperature=0.8, max_tokens=2500)
        try:
            return parse_script(text)
        except ValueError:
            print(f"сценарий не разобран (попытка {attempt + 1}): {text[:300]!r}", flush=True)
    raise ValueError("сценарий не по формату")


def parse_script(text):
    """JSON из ответа модели: без размышлений <think> и без обёртки ```json."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("нет JSON")
    doc = json.loads(text[start:end + 1])
    if not isinstance(doc, dict):
        raise ValueError("не объект")
    script = {key: " ".join(str(doc.get(key) or "").split())[:limit]
              for key, limit in SCRIPT_FIELDS.items()}
    if not script["frame_prompt"] or not script["video_prompt"]:
        raise ValueError("нет промптов")
    return script


def edit_script(client, pid, fields):
    """Правка сценария руками: название и промпты моделей."""
    base = folder_of(client)

    def change(project):
        script = project["script"]
        if script["status"] == "working":
            raise Problem(409, "Сценарий ещё пишется")
        for key in EDITABLE:
            if key in fields:
                value = " ".join(str(fields[key] or "").split())[:SCRIPT_FIELDS[key]]
                if not value and key in ("frame_prompt", "video_prompt"):
                    raise Problem(400, "Промпт кадра и промпт движения не могут быть пустыми")
                script[key] = value
    return update(base, pid, change)


# ------------------------------------------------------------ 2. кадры
def draw(client, pid, kind):
    """Qwen-Image рисует варианты первого кадра или, правкой выбранного
    первого кадра, варианты последнего: тогда герои и стиль совпадают."""
    if kind not in ("start", "end"):
        raise Problem(400, "Какой кадр: start или end")
    base = folder_of(client)

    def mark(project):
        if project["script"]["status"] != "ready":
            raise Problem(409, "Сначала нужен сценарий")
        if kind == "end" and not project["frames"]["chosen_start"]:
            raise Problem(409, "Сначала выберите первый кадр")
        if kind == "end" and not project["script"]["end_prompt"]:
            raise Problem(409, "В сценарии нет описания последнего кадра")
        _begin(project, "frames")
        project["frames"]["doing"] = kind
    project = update(base, pid, mark)
    script, chosen = project["script"], project["frames"]["chosen_start"]

    def work():
        try:
            ai = looma.for_client(client or None)
            if kind == "start":
                pngs = ai.image(FRAME_MODEL, script["frame_prompt"], n=START_VARIANTS,
                                size=FORMATS[project["format"]])
            else:
                first = store.get(_key(base, pid, f"{chosen}.png"))
                pngs = ai.image(FRAME_MODEL, script["end_prompt"], images=[first],
                                n=END_VARIANTS)
            names = []
            for png in pngs:
                name = f"{kind}-{uuid.uuid4().hex[:8]}"
                store.put(_key(base, pid, f"{name}.png"), png, "image/png")
                names.append(name)
            if not names:
                raise Problem(502, "Модель не вернула ни одного кадра")
        except Exception as exc:
            update(base, pid, _fail("frames", exc))
            return

        def done(p):
            frames = p["frames"]
            frames[kind] = (names + frames[kind])[:MAX_FRAMES]
            frames.update(status="ready", error="", doing="")
            if kind == "start" and frames["chosen_start"] not in frames["start"]:
                frames["chosen_start"] = names[0]
            if kind == "end" and not frames["chosen_end"]:
                frames["chosen_end"] = names[0]
        done_project = update(base, pid, done)
        if done_project["auto"] and kind == "start":
            _auto(client, pid, "shoot")

    _background(pid, "frames", work)
    return project


def choose(client, pid, start=None, end=None):
    """Выбрать первый кадр и последний (`end=""`: без последнего кадра).
    Другой первый кадр сбрасывает последние: они нарисованы из прежнего."""
    base = folder_of(client)

    def change(project):
        frames = project["frames"]
        if frames["status"] == "working":
            raise Problem(409, "Кадры ещё рисуются")
        if start is not None:
            if start not in frames["start"]:
                raise Problem(400, "Такого кадра нет")
            if start != frames["chosen_start"]:
                frames.update(chosen_start=start, end=[], chosen_end="")
        if end is not None:
            if end and end not in frames["end"]:
                raise Problem(400, "Такого кадра нет")
            frames["chosen_end"] = end
    return update(base, pid, change)


# ------------------------------------------------------------ 3. ролик
def shoot(client, pid):
    """Задание MiniMax-H3: ролик начинается выбранным первым кадром и, если
    выбран, заканчивается последним. Если платформа отказала, дубль остаётся с ошибкой."""
    base = folder_of(client)
    with lock_of(pid):
        project = load(base, pid)
        frames, script = project["frames"], project["script"]
        if not frames["chosen_start"]:
            raise Problem(409, "Сначала выберите первый кадр")
        if any(t["status"] == "working" for t in project["takes"]):
            raise Problem(409, "Ролик уже снимается")
        if len(project["takes"]) >= MAX_TAKES:
            raise Problem(409, f"Не больше {MAX_TAKES} дублей в проекте. Начните новый")
        conditions = [{"type": "image", "role": "keyframe", "frame_index": 0,
                       "uri": _data_url(store.get(_key(base, pid, f"{frames['chosen_start']}.png")))}]
        if frames["chosen_end"]:
            conditions.append({"type": "image", "role": "keyframe", "frame_index": -1,
                               "uri": _data_url(store.get(_key(base, pid, f"{frames['chosen_end']}.png")))})
        seed = random.randint(1, 2**31 - 1)
        take = {"id": uuid.uuid4().hex[:8], "job": "", "status": "working", "progress": 0,
                "queued": True, "estimated_s": None, "started": _now(), "error": "",
                "seed": seed, "start": frames["chosen_start"], "end": frames["chosen_end"]}
        try:
            job = looma.for_client(client or None).video(
                VIDEO_MODEL, script["video_prompt"], seconds=project["seconds"],
                quality=project["quality"], size=FORMATS[project["format"]],
                conditions=conditions, seed=seed)
            take.update(job=job.id, estimated_s=job.doc.get("estimated_s"))
        except Exception as exc:
            take.update(status="failed", error=_reason(exc), queued=False)
        project["takes"].append(take)
        save(base, project)
        return project


def _data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _settle(base, project):
    """О дублях в работе спросить платформу (не чаще POLL_S). Готовый дубль
    забрать в хранилище и отметить событием `video` на клиента."""
    if not any(t["status"] == "working" and t["job"] for t in project["takes"]):
        return project
    lock = lock_of(project["id"])
    if not lock.acquire(blocking=False):
        return project                  # забирает другой запрос
    try:
        project = load(base, project["id"])
        changed = False
        for take in project["takes"]:
            if take["status"] != "working" or not take["job"]:
                continue
            if time.monotonic() - _checked.get(take["id"], 0) < POLL_S:
                continue
            _checked[take["id"]] = time.monotonic()
            try:
                job = Video(looma, {"id": take["job"]}).refresh()
                if job.status == "completed":
                    store.put(_key(base, project["id"], f"take-{take['id']}.mp4"),
                              job.content(), "video/mp4")
                    take.update(status="ready", progress=100, queued=False, finished=_now())
                    _charge(project, take)
                elif job.status in ("failed", "cancelled"):
                    error = job.doc.get("error")
                    take.update(status="failed", queued=False, error=(
                        error.get("message") if isinstance(error, dict) else error)
                        or "Платформа не сняла ролик")
                else:
                    take.update(progress=int(job.doc.get("progress") or 0),
                                queued=job.status == "queued",
                                estimated_s=job.doc.get("estimated_s") or take["estimated_s"])
                changed = True
            except LoomaError as exc:
                print(f"дубль {take['id']}: платформа не ответила: {exc.message}", flush=True)
        if changed:
            save(base, project)
        return project
    finally:
        lock.release()


def _charge(project, take):
    if not project["client"]:
        return                          # не клиент каталога (у себя, `looma dev`)
    try:
        looma.charge("video", project["client"], id=take["id"])
    except LoomaError as exc:
        print(f"событие video не записалось: {exc.message}", flush=True)


# ------------------------------------------------------------ без остановок
def _auto(client, pid, step):
    """Режим «без остановок»: сценарий → первый вариант кадра → съёмка."""
    try:
        if step == "start":
            draw(client, pid, "start")
        else:
            shoot(client, pid)
    except Problem as exc:
        print(f"проект {pid}: автошаг {step} не начался: {exc.message}", flush=True)


# ------------------------------------------------------------ для страницы
def stage(project):
    if project["takes"]:
        return "video"
    if project["frames"]["start"] or project["frames"]["status"] != "idle":
        return "frames"
    return "script"


def _media(project, name, ext="png"):
    return f"/media/{project['id']}/{name}.{ext}" if name else ""


def public(project):
    """Проект для страницы: без псевдонима клиента и номеров заданий платформы."""
    frames = project["frames"]
    return {
        "id": project["id"], "idea": project["idea"], "format": project["format"],
        "seconds": project["seconds"], "quality": project["quality"],
        "auto": project["auto"], "created": project["created"],
        "updated": project.get("updated", project["created"]), "stage": stage(project),
        "script": dict(project["script"]),
        "frames": {"status": frames["status"], "error": frames["error"],
                   "doing": frames["doing"], "started": frames.get("started"),
                   "chosen_start": frames["chosen_start"], "chosen_end": frames["chosen_end"],
                   "start": [{"id": f, "url": _media(project, f)} for f in frames["start"]],
                   "end": [{"id": f, "url": _media(project, f)} for f in frames["end"]]},
        "takes": [{"id": t["id"], "status": t["status"], "progress": t["progress"],
                   "queued": t.get("queued", False), "estimated_s": t["estimated_s"],
                   "started": t["started"], "finished": t.get("finished"),
                   "error": t["error"], "start": _media(project, t["start"]),
                   "url": _media(project, f"take-{t['id']}", "mp4")
                   if t["status"] == "ready" else ""}
                  for t in project["takes"]],
    }


def summary(project):
    """Карточка проекта в галерее."""
    frames, takes = project["frames"], project["takes"]
    ready = [t for t in takes if t["status"] == "ready"]
    working = (project["script"]["status"] == "working" or frames["status"] == "working"
               or any(t["status"] == "working" for t in takes))
    failed = not working and not ready and (
        project["script"]["status"] == "failed" or frames["status"] == "failed"
        or (takes and takes[-1]["status"] == "failed"))
    cover = frames["chosen_start"] or (frames["start"][0] if frames["start"] else "")
    return {"id": project["id"], "title": project["script"]["title"] or project["idea"][:60],
            "idea": project["idea"], "format": project["format"],
            "seconds": project["seconds"], "created": project["created"],
            "stage": stage(project),
            "state": "working" if working else "ready" if ready else
                     "failed" if failed else "draft",
            "cover": _media(project, cover),
            "video": _media(project, f"take-{ready[-1]['id']}", "mp4") if ready else "",
            "takes": len(ready)}


def media_key(client, pid, name):
    """Ключ файла проекта по имени из адреса. Для чужого имени None."""
    if _ID.match(pid or "") and _MEDIA.match(name or ""):
        return _key(folder_of(client), pid, name)
    return None
