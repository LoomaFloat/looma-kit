"""Skills годятся для всех агентов открытого формата: в шапке SKILL.md только поля
спецификации Agent Skills (Claude.ai и Skills API отвергают остальные), имя совпадает
с папкой, описание укладывается в самый строгий предел (200 знаков у Claude.ai),
ссылки на справку ведут в существующие файлы, сам SKILL.md короче 500 строк.

    python tests/check_skills.py skills
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DESCRIPTION_MAX = 200
COMPATIBILITY_MAX = 500
LINES_MAX = 500


def frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """Шапка YAML без библиотеки: верхние ключи и вложенный словарь metadata."""
    if not text.startswith("---\n"):
        raise ValueError("нет шапки --- в начале файла")
    head, _, body = text[4:].partition("\n---\n")
    fields: Dict[str, object] = {}
    current = None
    for line in head.splitlines():
        if not line.strip():
            continue
        nested = re.match(r"^\s{2,}([\w-]+):\s*(.*)$", line)
        if nested and current == "metadata":
            value = nested.group(2).strip()
            fields.setdefault("metadata", {})[nested.group(1)] = value
            continue
        top = re.match(r"^([\w-]+):\s*(.*)$", line)
        if not top:
            raise ValueError(f"не разобрать строку шапки: {line!r}")
        current = top.group(1)
        fields[current] = top.group(2).strip() if top.group(2).strip() else {}
    return fields, body


def check(skill: Path) -> List[str]:
    problems: List[str] = []
    path = skill / "SKILL.md"
    if not path.is_file():
        return [f"{skill}: нет SKILL.md"]
    text = path.read_text(encoding="utf-8")
    try:
        fields, body = frontmatter(text)
    except ValueError as exc:
        return [f"{path}: {exc}"]
    extra = set(fields) - ALLOWED
    if extra:
        problems.append(f"{path}: лишние поля шапки {sorted(extra)}")
    name = str(fields.get("name") or "")
    if not NAME.match(name) or len(name) > 64:
        problems.append(f"{path}: name {name!r} — a-z, цифры, дефис, до 64")
    if name != skill.name:
        problems.append(f"{path}: name {name!r} не совпадает с папкой {skill.name!r}")
    description = str(fields.get("description") or "")
    if not description or len(description) > DESCRIPTION_MAX:
        problems.append(f"{path}: description {len(description)} знаков, нужно 1–{DESCRIPTION_MAX}")
    if len(str(fields.get("compatibility") or "")) > COMPATIBILITY_MAX:
        problems.append(f"{path}: compatibility длиннее {COMPATIBILITY_MAX}")
    metadata = fields.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
            re.match(r'^".*"$', str(v)) for v in metadata.values()):
        problems.append(f"{path}: metadata — словарь строк в кавычках")
    if text.count("\n") > LINES_MAX:
        problems.append(f"{path}: длиннее {LINES_MAX} строк — справку в references/")
    for link in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", body):
        if "://" in link:
            continue
        if not (skill / link).exists():
            problems.append(f"{path}: ссылка на несуществующий {link}")
    for ref in sorted(skill.glob("references/*.md")):
        for link in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", ref.read_text(encoding="utf-8")):
            if "://" not in link and not (ref.parent / link).exists():
                problems.append(f"{ref}: ссылка на несуществующий {link}")
    return problems


def main(argv: List[str]) -> int:
    root = Path(argv[0] if argv else "skills")
    problems = [p for skill in sorted(root.iterdir()) if skill.is_dir() for p in check(skill)]
    for problem in problems:
        print(problem)
    print("skills в порядке" if not problems else f"проблем: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
