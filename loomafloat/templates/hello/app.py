"""Самый простой сервис на Looma Float: страница, которая знает, где живёт и
кто зашёл (клиента маркетплейса платформа называет в X-Looma-Client)."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        about = {
            "app": os.environ.get("LOOMA_APP", "{{name}}"),
            "environment": os.environ.get("LOOMA_ENVIRONMENT", "local"),
            "release": os.environ.get("LOOMA_RELEASE", "—"),
            "path": self.path,
            # Псевдоним клиента у этого сервиса: без почты и имени.
            "client": self.headers.get("X-Looma-Client"),
        }
        if self.path.startswith("/health"):
            body, kind = b"ok", "text/plain; charset=utf-8"
        elif self.path.startswith("/api"):
            body, kind = json.dumps(about, ensure_ascii=False).encode(), "application/json"
        else:
            who = (f"вы — клиент маркетплейса {about['client']}" if about["client"]
                   else "вы зашли без входа в маркетплейс")
            body = (f"<h1>Сервис {about['app']} работает</h1>"
                    f"<p>окружение {about['environment']}, релиз {about['release']}</p>"
                    f"<p>{who}</p>").encode()
            kind = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"слушаю 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
