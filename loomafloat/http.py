"""HTTP до API Looma на одной стандартной библиотеке.

Ответ с ошибкой — исключение `LoomaError` с текстом сервера: API отвечает
по-русски и по делу («приложения hello нет», «нужно право 'deploy'»), и
показывать его автору лучше любого своего пересказа.

429 — подождать `Retry-After` и повторить (до трёх раз): лимит запросов —
нормальное состояние, а не ошибка программы. Обрыв связи повторяется у
чтений; у записей — только когда соединение не установилось (отказ, DNS,
TLS-рукопожатие): тогда запрос до сервера точно не дошёл, а повторить
дошедшую запись — значит, может быть, сделать её дважды.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

USER_AGENT = "loomafloat/0.1"
RETRIES_429 = 3
RETRIES_NETWORK = 3


class LoomaError(RuntimeError):
    """Отказ API: код и причина словами."""

    def __init__(self, status: int, message: str, kind: str = "") -> None:
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message
        #: Тип ошибки API (`error.type`): `authorization_pending`, `rate_limit_exceeded`, …
        self.kind = kind


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_DIRECT = urllib.request.build_opener(_NoRedirect())


def _kind(body: bytes) -> str:
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return ""
    return str(error.get("type") or "") if isinstance(error, dict) else ""


def _reason(body: bytes, status: int) -> str:
    try:
        doc = json.loads(body)
    except ValueError:
        text = body.decode(errors="replace").strip()
        return text[:500] or f"HTTP {status}"
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)
    if isinstance(doc, dict) and doc.get("detail"):
        return str(doc["detail"])
    return str(error or doc)[:500]


def _not_sent(exc: Exception) -> bool:
    """Соединение не установилось — запрос сервер не видел."""
    reason = getattr(exc, "reason", None)
    if not isinstance(exc, urllib.error.URLError) or reason is None:
        return False
    return (isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            or "handshake" in str(reason).lower())


def request(method: str, url: str, *, key: str = "", body: Optional[bytes] = None,
            headers: Optional[Dict[str, str]] = None, timeout: float = 60.0,
            follow: bool = True) -> Tuple[int, Dict[str, str], bytes]:
    """(код, заголовки, тело). 4xx/5xx — LoomaError; 3xx без `follow` —
    ответ как есть (ссылка на файл — в Location)."""
    outgoing = {"User-Agent": USER_AGENT, **(headers or {})}
    if key:
        outgoing["Authorization"] = f"Bearer {key}"
    reading = method.upper() in ("GET", "HEAD")
    attempt = 0
    while True:
        attempt += 1
        call = urllib.request.Request(url, data=body, method=method.upper(), headers=outgoing)
        opener = urllib.request.build_opener() if follow else _DIRECT
        try:
            with opener.open(call, timeout=timeout) as answer:
                return answer.status, dict(answer.headers), answer.read()
        except urllib.error.HTTPError as exc:
            data = exc.read() or b""
            if not follow and 300 <= exc.code < 400:
                return exc.code, dict(exc.headers or {}), data
            if exc.code == 429 and attempt <= RETRIES_429:
                time.sleep(min(60.0, float((exc.headers or {}).get("Retry-After") or 2)))
                continue
            raise LoomaError(exc.code, _reason(data, exc.code), _kind(data)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if (reading or _not_sent(exc)) and attempt <= RETRIES_NETWORK:
                time.sleep(2 * attempt)
                continue
            reason = getattr(exc, "reason", exc)
            raise LoomaError(0, f"нет связи с {url.split('/')[2]}: {reason}") from None


def open_stream(method: str, url: str, *, key: str = "", body: Optional[bytes] = None,
                headers: Optional[Dict[str, str]] = None, timeout: float = 600.0):
    """Ответ, который читают по мере прихода (SSE чата). Отказ — LoomaError
    до первого байта."""
    outgoing = {"User-Agent": USER_AGENT, **(headers or {})}
    if key:
        outgoing["Authorization"] = f"Bearer {key}"
    call = urllib.request.Request(url, data=body, method=method.upper(), headers=outgoing)
    try:
        return urllib.request.build_opener().open(call, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise LoomaError(exc.code, _reason(exc.read() or b"", exc.code)) from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise LoomaError(0, f"нет связи с {url.split('/')[2]}: "
                            f"{getattr(exc, 'reason', exc)}") from None


def get_json(url: str, *, key: str = "", timeout: float = 60.0):
    _status, _head, body = request("GET", url, key=key, timeout=timeout)
    return json.loads(body) if body else None


def send_json(method: str, url: str, payload=None, *, key: str = "", timeout: float = 60.0,
              headers: Optional[Dict[str, str]] = None):
    data = json.dumps(payload if payload is not None else {}).encode()
    _status, _head, body = request(method, url, key=key, body=data, timeout=timeout,
                                   headers={"Content-Type": "application/json", **(headers or {})})
    return json.loads(body) if body else None


__all__ = ["LoomaError", "get_json", "open_stream", "request", "send_json"]
