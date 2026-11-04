"""Чат-ассистент на Looma Float: страница и потоковый ответ большой модели.

    GET  /           страница чата
    POST /api/chat   {"messages": [{"role": "user", "content": "…"}, …]} → текст по кускам
    POST /api/ask    {"question": "…"} → {"answer": "…"} — для агентов (инструмент MCP ask)

История разговора живёт в браузере и приходит целиком с каждым вопросом:
процессу нечего помнить, и сон без запросов разговор не теряет.

Пришёл клиент маркетплейса (X-Looma-Client) — вызов модели записывается на
него, а каждый ответ отмечается событием `answer` (раздел events looma.yaml).
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loomafloat import Looma, LoomaError, client_of

MODEL = os.environ.get("ASSISTANT_MODEL", "Qwen3.8-Flash-Next-NVFP4")
SYSTEM = os.environ.get("ASSISTANT_SYSTEM", "Ты — дружелюбный ассистент. Отвечай по делу, "
                                            "на языке вопроса.")
MAX_MESSAGES = 40
MAX_CHARS = 32_000
looma = Looma()

PAGE = """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{name}}</title>
<style>
 body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f6f4;color:#222}
 main{max-width:760px;margin:0 auto;padding:16px;display:flex;flex-direction:column;height:100vh;box-sizing:border-box}
 #log{flex:1;overflow:auto;padding:8px 0}
 .m{white-space:pre-wrap;padding:10px 14px;border-radius:12px;margin:8px 0;max-width:85%}
 .user{background:#222;color:#fff;margin-left:auto}.assistant{background:#fff;border:1px solid #e3e3df}
 form{display:flex;gap:8px}textarea{flex:1;font:inherit;padding:10px;border-radius:10px;border:1px solid #ccc;resize:none}
 button{font:inherit;padding:0 18px;border-radius:10px;border:0;background:#222;color:#fff;cursor:pointer}
 button:disabled{opacity:.5}
</style>
<main><h2>{{name}}</h2><div id="log"></div>
<form id="f"><textarea id="q" rows="2" placeholder="Спросите что-нибудь…"></textarea><button id="b">→</button></form></main>
<script>
const log=document.getElementById('log'),q=document.getElementById('q'),b=document.getElementById('b');
const messages=[];
function add(role,text){const d=document.createElement('div');d.className='m '+role;d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;return d}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const text=q.value.trim();if(!text)return;
 q.value='';b.disabled=true;messages.push({role:'user',content:text});add('user',text);const out=add('assistant','…');
 try{const r=await fetch('api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages})});
  if(!r.ok){out.textContent='Ошибка: '+await r.text();return}
  const reader=r.body.getReader(),dec=new TextDecoder();let answer='';
  for(;;){const {done,value}=await reader.read();if(done)break;answer+=dec.decode(value,{stream:true});out.textContent=answer;log.scrollTop=log.scrollHeight}
  messages.push({role:'assistant',content:answer})}
 catch(err){out.textContent='Ошибка связи: '+err}finally{b.disabled=false;q.focus()}};
q.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();document.getElementById('f').requestSubmit()}});
</script></html>"""


def clean(messages):
    """Только роли user/assistant и строки: системную подсказку ставит сервер."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("нужен непустой список messages")
    out = [{"role": m["role"], "content": m["content"]} for m in messages[-MAX_MESSAGES:]
           if isinstance(m, dict) and m.get("role") in ("user", "assistant")
           and isinstance(m.get("content"), str)]
    if not out or sum(len(m["content"]) for m in out) > MAX_CHARS:
        raise ValueError(f"разговор пустой или длиннее {MAX_CHARS} знаков")
    return [{"role": "system", "content": SYSTEM}] + out


class Handler(BaseHTTPRequestHandler):
    def _plain(self, status, text):
        body = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._plain(200, "ok")
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.startswith("/api/ask"):
            return self._ask()
        if not self.path.startswith("/api/chat"):
            return self._plain(404, "нет такого пути")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            messages = clean(json.loads(self.rfile.read(length) or b"{}").get("messages"))
        except (ValueError, KeyError, AttributeError) as exc:
            return self._plain(400, str(exc))
        client = client_of(self.headers)
        try:
            pieces = looma.for_client(client).chat_stream(MODEL, messages, max_tokens=2048)
            first = next(pieces, "")
        except LoomaError as exc:
            return self._plain(502, f"модель не ответила: {exc.message}")
        # Текст — кусками по мере генерации; конец ответа — закрытое соединение.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(first.encode())
            self.wfile.flush()
            for piece in pieces:
                self.wfile.write(piece.encode())
                self.wfile.flush()
        except LoomaError as exc:
            self.wfile.write(f"\n[ответ оборвался: {exc.message}]".encode())
            return
        except (BrokenPipeError, ConnectionResetError):
            return
        if client:
            try:
                looma.charge("answer", client)
            except LoomaError as exc:
                print(f"событие answer не записалось: {exc.message}", flush=True)

    def _ask(self):
        """Один вопрос — один ответ целиком: так его ждёт агент через MCP."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            question = str(json.loads(self.rfile.read(length) or b"{}").get("question") or "")
        except (ValueError, AttributeError) as exc:
            return self._plain(400, str(exc))
        if not question.strip():
            return self._plain(400, "нужен question")
        client = client_of(self.headers)
        try:
            answer = looma.for_client(client).chat(MODEL, clean([{"role": "user",
                                                                   "content": question}]),
                                                   max_tokens=2048)
        except LoomaError as exc:
            return self._plain(502, f"модель не ответила: {exc.message}")
        if client:                      # ответ готов — событие, потом отправка
            try:
                looma.charge("answer", client)
            except LoomaError as exc:
                print(f"событие answer не записалось: {exc.message}", flush=True)
        body = json.dumps({"answer": answer}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"ассистент на {MODEL} слушает 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
