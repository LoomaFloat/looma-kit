"""Студия картинок на Looma: генерация, правка и галерея в своём бакете.

    GET  /                страница
    POST /api/generate    {"prompt", "size"?, "seed"?}       → {"id"}
    POST /api/edit        {"id", "prompt"}                    → {"id"} — правка картинки галереи
    POST /api/draw        {"prompt", "size"?}                 → PNG — для агентов (инструмент MCP draw)
    GET  /api/images      последние картинки с подписями
    GET  /img/<id>.png    сама картинка

Картинки — в бакете приложения (платформа даёт `LOOMA_BUCKET` и ключи S3), рядом
с каждой — её подпись (`<id>.json`). Без бакета — в папке `gallery/`.

У каждого клиента маркетплейса (X-Looma-Client) — своя галерея: чужую картинку
не увидеть и не поправить. Посетители без входа делят одну общую.
"""

import json
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from loomafloat import Looma, LoomaError, client_of

MODEL = os.environ.get("STUDIO_MODEL", "Qwen-Image-2.1")
SIZES = ("1024x1024", "1344x768", "768x1344")
looma = Looma()
_ID = re.compile(r"^[0-9a-f]{12}-[0-9a-f]{6}$")


class Bucket:
    """Бакет приложения, а локально — папка."""

    def __init__(self):
        self.name = os.environ.get("LOOMA_BUCKET", "")
        if self.name:
            import boto3
            from botocore.config import Config

            self.s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
                                   region_name=os.environ.get("AWS_REGION", "us-east-1"),
                                   config=Config(s3={"addressing_style": "path"}))
        else:
            self.dir = Path("gallery")
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


def save(png, prompt, parent=None, client=""):
    image_id = f"{int(time.time() * 1000):012x}-{uuid.uuid4().hex[:6]}"
    bucket.put(f"{image_id}.png", png, "image/png")
    bucket.put(f"{image_id}.json", json.dumps({"prompt": prompt, "parent": parent,
                                               "client": client or ""},
                                              ensure_ascii=False).encode(), "application/json")
    return image_id


def meta_of(image_id):
    try:
        return json.loads(bucket.get(f"{image_id}.json"))
    except Exception:
        return None


def mine(image_id, client):
    """Картинка этого клиента (или общая — у посетителя без входа)."""
    meta = meta_of(image_id) if _ID.match(image_id or "") else None
    return meta is not None and meta.get("client", "") == (client or "")


def gallery(client="", limit=60):
    ids = sorted((k[:-5] for k in bucket.keys() if k.endswith(".json")), reverse=True)
    out = []
    for image_id in ids:
        meta = meta_of(image_id) or {}
        if meta.get("client", "") != (client or ""):
            continue
        out.append({"id": image_id, "prompt": meta.get("prompt", ""), "parent": meta.get("parent")})
        if len(out) >= limit:
            break
    return out


PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{name}}</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#111;color:#eee}
 main{max-width:1100px;margin:0 auto;padding:16px}
 form{display:flex;gap:8px;flex-wrap:wrap}input,select,button{font:inherit;padding:10px;border-radius:10px;border:1px solid #444;background:#1c1c1c;color:#eee}
 input{flex:1;min-width:240px}button{background:#eee;color:#111;border:0;cursor:pointer}button:disabled{opacity:.5}
 #status{margin:10px 0;color:#aaa;min-height:1.5em}
 #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
 figure{margin:0;background:#1c1c1c;border-radius:12px;overflow:hidden}img{width:100%;display:block}
 figcaption{padding:8px 10px;font-size:13px;color:#bbb}figcaption a{color:#9cf;cursor:pointer}
</style>
<main><h2>{{name}}</h2>
<form id="f"><input id="p" placeholder="Что нарисовать?" required>
<select id="s"><option>1024x1024</option><option>1344x768</option><option>768x1344</option></select>
<button id="b">Нарисовать</button></form><div id="status"></div><div id="grid"></div></main>
<script>
const $=id=>document.getElementById(id);
async function call(url,body){$('b').disabled=true;$('status').textContent='Рисую… (до минуты, первая картинка дольше)';
 try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const t=await r.text();if(!r.ok){$('status').textContent='Ошибка: '+t;return}$('status').textContent='';load()}
 catch(e){$('status').textContent='Ошибка связи: '+e}finally{$('b').disabled=false}}
async function load(){const r=await fetch('api/images');const list=await r.json();$('grid').innerHTML='';
 for(const it of list){const f=document.createElement('figure');const img=new Image();img.loading='lazy';img.src='img/'+it.id+'.png';
  const c=document.createElement('figcaption');c.textContent=it.prompt+' ';const a=document.createElement('a');a.textContent='поправить';
  a.onclick=()=>{const p=prompt('Что поменять?');if(p)call('api/edit',{id:it.id,prompt:p})};c.appendChild(a);f.append(img,c);$('grid').appendChild(f)}}
$('f').onsubmit=e=>{e.preventDefault();call('api/generate',{prompt:$('p').value,size:$('s').value})};load();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body, kind="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._send(200, "ok", "text/plain; charset=utf-8")
        client = client_of(self.headers) or ""
        if self.path.startswith("/api/images"):
            return self._send(200, gallery(client))
        if self.path.startswith("/img/"):
            found = re.match(r"^/img/([0-9a-f-]+)\.png$", self.path)
            try:
                if found and mine(found.group(1), client):
                    return self._send(200, bucket.get(f"{found.group(1)}.png"), "image/png")
            except Exception:
                pass
            return self._send(404, "нет такой картинки", "text/plain; charset=utf-8")
        self._send(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            prompt = str(body.get("prompt") or "").strip()[:2000]
            if not prompt:
                raise ValueError("нужно описание")
            client = client_of(self.headers) or ""
            ai = looma.for_client(client or None)            # вызов — на клиента
            if self.path.startswith("/api/draw"):
                size = body.get("size") if body.get("size") in SIZES else SIZES[0]
                png = ai.image(MODEL, prompt, size=size)[0]
                save(png, prompt, client=client)
                return self._send(200, png, "image/png")
            if self.path.startswith("/api/generate"):
                size = body.get("size") if body.get("size") in SIZES else SIZES[0]
                extra = {"seed": int(body["seed"])} if str(body.get("seed", "")).isdigit() else {}
                png = ai.image(MODEL, prompt, size=size, **extra)[0]
                return self._send(200, {"id": save(png, prompt, client=client)})
            if self.path.startswith("/api/edit"):
                parent = str(body.get("id") or "")
                if not mine(parent, client):
                    raise ValueError("какую картинку править?")
                png = ai.image(MODEL, prompt, images=[bucket.get(f"{parent}.png")])[0]
                return self._send(200, {"id": save(png, prompt, parent, client)})
            return self._send(404, "нет такого пути", "text/plain; charset=utf-8")
        except ValueError as exc:
            return self._send(400, str(exc), "text/plain; charset=utf-8")
        except LoomaError as exc:
            return self._send(502, f"модель не ответила: {exc.message}",
                              "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    where = f"бакет {bucket.name}" if bucket.name else "папка gallery/"
    print(f"студия на {MODEL}, картинки — {where}, слушаю 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
