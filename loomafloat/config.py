"""Где CLI помнит адрес API и ключ: `~/.config/looma/config.json`.

Файл — только владельцу (0600): в нём ключ API. Переменные окружения сильнее
файла — так ключ приходит в CI и в процессы сервиса:

    LOOMA_API_KEY     ключ проекта (`looma_sk_…`)
    LOOMA_APP_TOKEN   токен сервиса внутри его процесса (`looma_app_…`)
    LOOMA_API_URL     адрес API; внутри сервиса — с `/v1` на конце
    LOOMA_CONFIG      другой файл настроек
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_API = "https://api.loomafloat.ru"
#: Где выпускают ключ проекта для CI: кабинет автора, раздел «Подключение».
KEYS_PAGE = "https://console.loomafloat.ru/connect"


def path() -> Path:
    own = os.environ.get("LOOMA_CONFIG", "").strip()
    if own:
        return Path(own).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "looma" / "config.json"


def load() -> dict:
    try:
        doc = json.loads(path().read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save(doc: dict) -> Path:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, target)
    return target


def forget() -> bool:
    try:
        path().unlink()
        return True
    except FileNotFoundError:
        return False


def base_url(url: str) -> str:
    """Адрес API без `/v1`: внутри сервиса LOOMA_API_URL — с ним."""
    url = (url or DEFAULT_API).strip().rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def api_url(explicit: Optional[str] = None) -> str:
    return base_url(explicit or os.environ.get("LOOMA_API_URL", "") or load().get("api_url", "")
                    or DEFAULT_API)


def user_key() -> str:
    """Ключ человека — для CLI и MCP. Токен сервиса (`LOOMA_APP_TOKEN`) — для
    кода сервиса: после `eval "$(looma dev env)"` CLI остаётся со своим ключом."""
    return (os.environ.get("LOOMA_API_KEY", "") or load().get("key", "")).strip()


def api_key(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get("LOOMA_API_KEY", "") or os.environ.get("LOOMA_APP_TOKEN", "")
            or load().get("key", "")).strip()


__all__ = ["DEFAULT_API", "KEYS_PAGE", "api_key", "api_url", "base_url", "forget", "load", "path",
           "save", "user_key"]
