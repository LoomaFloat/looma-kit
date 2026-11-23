"""Проект автора → архив исходников для релиза.

В архив не идёт то, что платформа соберёт сама или что не должно уезжать с
машины автора: `.git`, `node_modules`, виртуальные окружения, кэши, `.env`
(секреты — через `looma secrets`, а не файлом в образе). Свои исключения —
в `.loomaignore`, по строке на шаблон, как в `.gitignore` без отрицаний:
`build/`, `*.log`, `data/raw`.

Ссылки (symlink) не кладутся: платформа их всё равно отвергнет, а ссылка
наружу проекта — это файл, который автор не собирался отправлять.
"""

from __future__ import annotations

import io
import re
import tarfile
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple

MANIFEST = "looma.yaml"
IGNORE_FILE = ".loomaignore"
#: Предел архива — как у платформы (`LOOMA_APP_SOURCE_MAX_BYTES`).
MAX_BYTES = 100 * 1024 * 1024
DEFAULT_IGNORE = (".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__",
                  ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store", ".env", ".env.*",
                  "*.pyc", ".next", ".idea", ".vscode", ".looma")
_NAME = re.compile(r"^name\s*:\s*[\"']?([a-z][a-z0-9-]{0,39})[\"']?\s*(#.*)?$", re.MULTILINE)


class PackError(ValueError):
    """Проект не упаковать — словами для автора."""


def manifest_name(root: Path) -> Optional[str]:
    """Имя сервиса из `looma.yaml` (`name: …`) без разбора YAML целиком."""
    try:
        found = _NAME.search((root / MANIFEST).read_text(encoding="utf-8"))
    except OSError:
        return None
    return found.group(1) if found else None


def patterns(root: Path) -> List[str]:
    own = []
    try:
        for line in (root / IGNORE_FILE).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                own.append(line)
    except OSError:
        pass
    return list(DEFAULT_IGNORE) + own


def ignored(relative: PurePosixPath, rules: List[str]) -> bool:
    text = relative.as_posix()
    for rule in rules:
        bare = rule.strip("/")
        if "/" in bare:
            if fnmatch(text, bare) or text.startswith(bare + "/"):
                return True
        elif any(fnmatch(part, bare) for part in relative.parts):
            return True
    return False


def pack(root: Path) -> Tuple[bytes, List[str], List[str]]:
    """(архив .tar.gz, что вошло, что пропущено из ссылок)."""
    root = root.resolve()
    if not (root / MANIFEST).is_file():
        raise PackError(f"в {root} нет {MANIFEST}: начните с `looma init`")
    rules = patterns(root)
    included, skipped = [], []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if ignored(relative, rules):
                continue
            if path.is_symlink():
                skipped.append(relative.as_posix())
                continue
            if path.is_file():
                info = archive.gettarinfo(str(path), arcname=relative.as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
                included.append(relative.as_posix())
                if buffer.tell() > MAX_BYTES:
                    raise PackError(f"архив больше {MAX_BYTES // 2**20} МБ: лишнее — в "
                                    f"{IGNORE_FILE}")
    data = buffer.getvalue()
    if len(data) > MAX_BYTES:
        raise PackError(f"архив больше {MAX_BYTES // 2**20} МБ: лишнее — в {IGNORE_FILE}")
    return data, included, skipped


ENGINE_MANIFEST = "looma-engine.yaml"
EXTENSION_MANIFEST = "looma-extension.yaml"
#: Предел архива движка — как у площадки (`code_layers.MAX_UPLOAD`): только код.
ENGINE_MAX_BYTES = 16 * 1024 * 1024
#: Набор узлов ComfyUI (расширение движка `looma/comfyui`) крупнее: веб-файлы,
#: шрифты, примеры (`code_layers.MAX_UPLOAD_COMFY`).
COMFY_MAX_BYTES = 64 * 1024 * 1024
_COMFY_EXTENDS = re.compile(r"^extends:\s*['\"]?(looma/)?comfyui['\"]?\s*(#.*)?$", re.M)


def comfy_set(root: Path) -> bool:
    """Папка набора узлов ComfyUI: `looma-extension.yaml` с `extends: comfyui`."""
    try:
        text = (root / EXTENSION_MANIFEST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(_COMFY_EXTENDS.search(text))


def engine_kind(root: Path) -> str:
    """Что лежит в папке: движок (`looma-engine.yaml`) или расширение
    (`looma-extension.yaml`)."""
    engine, extension = (root / ENGINE_MANIFEST).is_file(), (root / EXTENSION_MANIFEST).is_file()
    if engine and extension:
        raise PackError(f"в {root} и {ENGINE_MANIFEST}, и {EXTENSION_MANIFEST}: оставьте один")
    if engine:
        return "engine"
    if extension:
        return "extension"
    raise PackError(f"в {root} нет {ENGINE_MANIFEST}: это не папка движка. Образец — "
                    "engines/echo в репозитории looma-kit")


def pack_engine(root: Path) -> Tuple[bytes, str, List[str]]:
    """Папка движка → (архив .tar.gz, engine или extension, что вошло).

    Всё лежит в одной верхней папке, как в архиве коммита GitHub: площадка
    разбирает оба одинаково. Исключения те же, что у сервиса, и `.loomaignore`
    папки движка."""
    root = root.resolve()
    kind = engine_kind(root)
    rules = patterns(root)
    limit = COMFY_MAX_BYTES if kind == "extension" and comfy_set(root) else ENGINE_MAX_BYTES
    included = []
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(root.rglob("*")):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if ignored(relative, rules) or path.is_symlink() or not path.is_file():
                continue
            info = archive.gettarinfo(str(path), arcname=f"engine/{relative.as_posix()}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                archive.addfile(info, handle)
            included.append(relative.as_posix())
            if buffer.tell() > limit:
                raise PackError(f"папка движка больше {limit // 2**20} МБ в архиве: "
                                f"веса и данные движок скачивает сам, лишнее — в {IGNORE_FILE}")
    data = buffer.getvalue()
    if len(data) > limit:
        raise PackError(f"папка движка больше {limit // 2**20} МБ в архиве: "
                        f"лишнее — в {IGNORE_FILE}")
    return data, kind, included


__all__ = ["COMFY_MAX_BYTES", "DEFAULT_IGNORE", "ENGINE_MANIFEST", "EXTENSION_MANIFEST", "MANIFEST", "MAX_BYTES",
           "PackError", "comfy_set", "engine_kind", "ignored", "manifest_name", "pack", "pack_engine",
           "patterns"]
