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
    """Имя приложения из `looma.yaml` (`name: …`) без разбора YAML целиком."""
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


__all__ = ["DEFAULT_IGNORE", "MANIFEST", "MAX_BYTES", "PackError", "ignored", "manifest_name",
           "pack", "patterns"]
