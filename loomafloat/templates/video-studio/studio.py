"""Видеостудия на Looma Float: ролик по описанию, галерея в своём бакете.

    GET  /                страница
    POST /api/videos      {"prompt"} → {"id"} — задание; ролик считается минуты
    GET  /api/videos      мои ролики: готовые и в работе (о тех, что в работе, — спросить платформу)
    GET  /video/<id>.mp4  сам ролик

Ролик считает платформа, процесс помнит только номер задания: сон процесса
ролик не теряет — следующий заход спросит платформу и заберёт готовый ролик в
бакет. У каждого клиента маркетплейса (X-Looma-Client) — своя галерея;
готовый ролик клиента — событие `video` для учёта (раздел events looma.yaml).
"""

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from loomafloat import Looma, LoomaError, Video, client_of

MODEL = os.environ.get("STUDIO_VIDEO_MODEL", "MiniMax-H3")
QUALITY = os.environ.get("STUDIO_VIDEO_QUALITY", "fast")
SECONDS = 5
looma = Looma()
_ID = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{6}$")
#: Один ролик забирается в бакет одним запросом, сколько бы вкладок ни спрашивали.
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class Bucket:
    """Бакет сервиса, а локально — папка."""

    def __init__(self):
        self.name = os.environ.get("LOOMA_BUCKET", "")
        if self.name:
            import boto3
            from botocore.config import Config

            self.s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
                                   region_name=os.environ.get("AWS_REGION", "us-east-1"),
                                   config=Config(s3={"addressing_style": "path"}))
        else:
            self.dir = Path("videos")
            self.dir.mkdir(exist_ok=True)

    def put(self, key, data, kind):
        if self.name:
            self.s3.put_object(Bucket=self.name, Key=key, Body=data, ContentType=kind)
        else:
            (self.dir / key).write_bytes(data)

    def get(self, key):
        if self.name:
            return self.s3.get_object(Bucket=self.name, Key=key)["Body"].read()
        return (self.dir / key).read_bytes()

    def keys(self):
        if self.name:
            pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.name)
            return [o["Key"] for page in pages for o in page.get("Contents", [])]
        return [p.name for p in self.dir.iterdir()]


bucket = Bucket()


def save_meta(meta):
    bucket.put(f"{meta['id']}.json", json.dumps(meta, ensure_ascii=False).encode(),
               "application/json")


def meta_of(video_id):
    if not _ID.match(video_id or ""):
        return None
    try:
        return json.loads(bucket.get(f"{video_id}.json"))
    except Exception:
        return None


def create(prompt, client):
    job = looma.for_client(client or None).video(MODEL, prompt, seconds=SECONDS, quality=QUALITY)
    video_id = f"{int(time.time() * 1000):012x}-{uuid.uuid4().hex[:6]}"
    save_meta({"id": video_id, "prompt": prompt, "job": job.id, "status": "working",
               "client": client or "", "created": int(time.time()), "error": ""})
    return video_id


def settle(meta):
    """Ролик в работе — спросить платформу; готов — забрать в бакет и отметить."""
    if meta.get("status") != "working":
        return meta
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(meta["id"], threading.Lock())
    if not lock.acquire(blocking=False):
        return meta                         # забирает другой запрос
    try:
        meta = meta_of(meta["id"]) or meta
        if meta.get("status") != "working":
            return meta
        job = Video(looma, {"id": meta["job"]}).refresh()
        if job.status == "completed":
            bucket.put(f"{meta['id']}.mp4", job.content(), "video/mp4")
            meta["status"] = "ready"
            if meta.get("client"):
                try:
                    looma.charge("video", meta["client"], id=meta["id"])
                except LoomaError as exc:
                    print(f"событие video не записалось: {exc.message}", flush=True)
        elif job.status in ("failed", "cancelled"):
            error = job.doc.get("error")
            meta.update(status="failed",
                        error=(error.get("message") if isinstance(error, dict) else error) or job.status)
        else:
            return meta
        save_meta(meta)
        return meta
    except LoomaError as exc:
        print(f"ролик {meta['id']}: платформа не ответила — {exc.message}", flush=True)
        return meta
    finally:
        lock.release()


def videos(client, limit=40):
    ids = sorted((k[:-5] for k in bucket.keys() if k.endswith(".json")), reverse=True)
    out = []
    for video_id in ids:
        meta = meta_of(video_id)
        if not meta or meta.get("client", "") != (client or ""):
            continue
        meta = settle(meta)
        out.append({k: meta.get(k) for k in ("id", "prompt", "status", "error", "created")})
        if len(out) >= limit:
            break
    return out


PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{name}}</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#111;color:#eee}
 main{max-width:1100px;margin:0 auto;padding:16px}
 form{display:flex;gap:8px;flex-wrap:wrap}input,button{font:inherit;padding:10px;border-radius:10px;border:1px solid #444;background:#1c1c1c;color:#eee}
 input{flex:1;min-width:240px}button{background:#eee;color:#111;border:0;cursor:pointer}button:disabled{opacity:.5}
 #status{margin:10px 0;color:#aaa;min-height:1.5em}
 #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
 figure{margin:0;background:#1c1c1c;border-radius:12px;overflow:hidden}video{width:100%;display:block;background:#000}
 .wait{aspect-ratio:16/9;display:grid;place-items:center;color:#aaa;font-size:14px;padding:12px;text-align:center}
 figcaption{padding:8px 10px;font-size:13px;color:#bbb}
</style>
<main><h2>{{name}}</h2>
<form id="f"><input id="p" placeholder="Что снять? Например: рыжая лиса бежит по снегу" required>
<button id="b">Снять ролик</button></form><div id="status"></div><div id="grid"></div></main>
<script>
const $=id=>document.getElementById(id);let timer=null;
async function load(){const r=await fetch('api/videos');if(!r.ok){$('status').textContent=await r.text();return}
 const list=await r.json();$('grid').innerHTML='';let working=false;
 for(const it of list){const f=document.createElement('figure');
  if(it.status==='ready'){const v=document.createElement('video');v.src='video/'+it.id+'.mp4';v.controls=true;v.preload='metadata';f.appendChild(v)}
  else{const w=document.createElement('div');w.className='wait';
   w.textContent=it.status==='failed'?'Не получилось: '+(it.error||'ошибка'):'Ролик снимается — это несколько минут';
   if(it.status==='working')working=true;f.appendChild(w)}
  const c=document.createElement('figcaption');c.textContent=it.prompt;f.appendChild(c);$('grid').appendChild(f)}
 clearTimeout(timer);if(working)timer=setTimeout(load,10000)}
$('f').onsubmit=async e=>{e.preventDefault();$('b').disabled=true;$('status').textContent='Отправляю…';
 try{const r=await fetch('api/videos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:$('p').value})});
  const t=await r.text();$('status').textContent=r.ok?'Задание принято: ролик появится здесь, когда будет готов.':'Ошибка: '+t;if(r.ok){$('p').value='';load()}}
 catch(err){$('status').textContent='Ошибка связи: '+err}finally{$('b').disabled=false}};
load();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, kind="application/json", extra=None):
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
        self.wfile.write(body)

    def _video(self, data):
        """Ролик — с поддержкой Range: Safari иначе не играет."""
        found = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if not found or not data:
            return self._send(200, data, "video/mp4", {"Accept-Ranges": "bytes"})
        start = int(found.group(1) or 0)
        end = min(int(found.group(2)) if found.group(2) else len(data) - 1, len(data) - 1)
        if start > end:
            return self._send(416, b"", "video/mp4", {"Content-Range": f"bytes */{len(data)}"})
        return self._send(206, data[start:end + 1], "video/mp4",
                          {"Accept-Ranges": "bytes",
                           "Content-Range": f"bytes {start}-{end}/{len(data)}"})

    def do_GET(self):
        client = client_of(self.headers) or ""
        if self.path.startswith("/health"):
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if self.path.startswith("/api/videos"):
            return self._send(200, videos(client))
        if self.path.startswith("/video/"):
            found = re.match(r"^/video/([0-9a-f-]+)\.mp4$", self.path)
            meta = meta_of(found.group(1)) if found else None
            if meta and meta.get("client", "") == client and meta.get("status") == "ready":
                try:
                    return self._video(bucket.get(f"{meta['id']}.mp4"))
                except Exception:
                    pass
            return self._send(404, "нет такого ролика", "text/plain; charset=utf-8")
        self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        if not self.path.startswith("/api/videos"):
            return self._send(404, "нет такого пути", "text/plain; charset=utf-8")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            prompt = str(json.loads(self.rfile.read(length) or b"{}").get("prompt") or "").strip()
            if not prompt:
                raise ValueError("нужно описание")
            return self._send(200, {"id": create(prompt[:2000], client_of(self.headers) or "")})
        except ValueError as exc:
            return self._send(400, str(exc), "text/plain; charset=utf-8")
        except LoomaError as exc:
            return self._send(502, f"модель не ответила: {exc.message}",
                              "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    where = f"бакет {bucket.name}" if bucket.name else "папка videos/"
    print(f"видеостудия на {MODEL}, ролики — {where}, слушаю 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
