"""Примеры в examples/: каждый пример является проектом, который `looma deploy` примет
как есть. Проверка без сети, как у шаблонов: манифест, архив релиза и `looma validate`
без входа. Потом настоящий запуск копии против подставного API: процесс встаёт и
отдаёт страницу."""

from __future__ import annotations

import json
import re
import shutil
import urllib.request
from pathlib import Path

import pytest

from loomafloat import ops
from loomafloat.pack import manifest_name, pack
from test_templates import _start, api  # noqa: F401  фикстура и запуск процесса

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
#: Папка → (имя в looma.yaml, файл веб-процесса, заголовок страницы).
CASES = {
    "assistant": ("looma-assistant", "app.py", "<title>Ассистент · Looma Float</title>"),
    "image-studio": ("looma-studio", "studio.py",
                     "<title>Студия изображений · Looma Float</title>"),
    "video-studio": ("looma-video", "studio.py", "<title>Видеостудия · Looma Float</title>"),
    "storyboard": ("storyboard", "app.py", "<title>Раскадровка</title>"),
}
#: Чего не должно быть в открытом репозитории.
INTERNAL = ("/Users/", "LoomaFloat/client", "devkit", "admin", "от Looma")


def _clean_copy(example: str, tmp_path: Path) -> Path:
    """Пример как в git: без копии SDK, кэшей и данных локального запуска."""
    target = tmp_path / example
    shutil.copytree(EXAMPLES / example, target, ignore=shutil.ignore_patterns(
        "loomafloat", "__pycache__", "*.pyc", "data", "gallery", "videos", ".env"))
    return target


def _models(manifest: str) -> set:
    return set(re.findall(r"\{\s*model:\s*([\w.-]+)\s*\}", manifest))


def test_every_example_folder_is_checked():
    folders = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir())
    assert folders == sorted(CASES)
    assert (EXAMPLES / "README.md").is_file()
    assert "examples/*/loomafloat/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.parametrize("example", sorted(CASES))
def test_every_example_is_a_valid_project(tmp_path, example):
    name, script, _title = CASES[example]
    root = _clean_copy(example, tmp_path)
    manifest = (root / "looma.yaml").read_text(encoding="utf-8")
    assert manifest_name(root) == name
    assert "gpu:" in manifest and "listing:" in manifest and "icon: icon.png" in manifest
    assert not re.search(r"\bprice\s*:", manifest)            # цены задаёт автор
    assert (root / "README.md").is_file() and (root / script).is_file()
    # Код импортирует SDK, а копии нет: `looma deploy` положит её сам.
    assert ops.missing_sdk(root)
    report = ops.validate(None, root)
    assert report["ok"], report["problems"]
    assert any("положит её сам" in w for w in report["warnings"])
    _data, files, skipped = pack(root)
    assert {"looma.yaml", "icon.png", script} <= set(files) and not skipped
    assert not any(f.startswith("loomafloat/") or "__pycache__" in f for f in files)
    # После копии SDK, как при `looma deploy`, она едет в релиз.
    ops.vendor(root)
    assert not ops.missing_sdk(root)
    assert "loomafloat/client.py" in pack(root)[1]


@pytest.mark.parametrize("example", sorted(CASES))
def test_examples_keep_internal_details_out(example):
    for path in (EXAMPLES / example).rglob("*"):
        if not path.is_file() or "loomafloat" in path.relative_to(EXAMPLES).parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue                                          # иконка
        for word in INTERNAL:
            assert word not in text, f"{path.relative_to(ROOT)}: {word}"


@pytest.mark.parametrize("example", sorted(CASES))
def test_every_example_starts_and_serves_its_page(tmp_path, api, example):  # noqa: F811
    base, _state = api
    _name, script, title = CASES[example]
    root = _clean_copy(example, tmp_path)
    ops.vendor(root)
    proc, url = _start(root, script, base)
    try:
        page = urllib.request.urlopen(url + "/", timeout=5).read().decode()
        assert title in page
        if example == "storyboard":
            # Модели конвейера те же, что в разделе gpu: иначе площадка ответит 403.
            config = json.loads(urllib.request.urlopen(url + "/api/config", timeout=5).read())
            manifest = (root / "looma.yaml").read_text(encoding="utf-8")
            assert set(config["models"].values()) == _models(manifest)
    finally:
        proc.kill()
