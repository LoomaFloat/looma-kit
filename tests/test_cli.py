"""CLI `looma` и SDK против подставного API: путь автора от входа до отката."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import fake_api
from loomafloat import Looma, LoomaError, cli, config
from loomafloat.pack import PackError, manifest_name, pack


@pytest.fixture
def api(tmp_path, monkeypatch):
    server, state = fake_api.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("LOOMA_CONFIG", str(tmp_path / "config.json"))
    for name in ("LOOMA_API_KEY", "LOOMA_APP_TOKEN", "LOOMA_API_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("loomafloat.client.Apps.wait_release",
                        _fast_wait(Looma.__init__), raising=False)
    yield base, state
    server.shutdown()


def _fast_wait(_):
    """Ожидание сборки без пауз: подставной API отвечает «готово» со второго раза."""
    def wait(self, app, number, **kw):
        while True:
            info = self.release_info(app, number)
            if info["status"] != "building":
                return info
    return wait


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def login(base, capsys):
    code, out, err = run(capsys, "--api", base, "login", "--key", fake_api.KEY)
    assert code == 0, err
    return out


def test_login_checks_the_key_and_keeps_it_private(api, capsys, tmp_path):
    base, _state = api
    code, _out, err = run(capsys, "--api", base, "login", "--key", "looma_sk_wrong")
    assert code == 1 and "ключ не подошёл" in err
    out = login(base, capsys)
    assert "author@example.ru" in out and "проект 7" in out
    saved = Path(os.environ["LOOMA_CONFIG"])
    assert json.loads(saved.read_text()) == {"api_url": base, "key": fake_api.KEY}
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    code, out, _ = run(capsys, "whoami")
    assert code == 0 and "deploy" in out
    run(capsys, "logout")
    code, _out, err = run(capsys, "whoami")
    assert code == 1 and "looma login" in err


def test_login_through_the_browser(api, capsys, monkeypatch):
    base, state = api
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "time", __import__("time"))
    code, out, err = run(capsys, "--api", base, "login")
    assert code == 0, err
    assert "ABCD-EFGH" in out and "https://console.example.ru/device?code=ABCD-EFGH" in out
    assert opened == ["https://console.example.ru/device?code=ABCD-EFGH"]
    # 503, «ждём подтверждения», ключ: сбой сервера вход не обрывает.
    assert state.device_polls == 3 and "author@example.ru" in out
    assert json.loads(Path(os.environ["LOOMA_CONFIG"]).read_text())["key"] == fake_api.KEY


def test_init_deploy_promote_rollback(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    monkeypatch.chdir(tmp_path)
    code, out, _ = run(capsys, "init", "bot", "--template", "hello")
    assert code == 0 and "looma deploy" in out
    root = tmp_path / "bot"
    assert "name: bot" in (root / "looma.yaml").read_text()
    assert "{{name}}" not in (root / "app.py").read_text()
    # Мусор и секреты в релиз не уезжают.
    (root / ".env").write_text("TOKEN=secret")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "i.js").write_text("x")
    (root / "debug.log").write_text("x")
    monkeypatch.chdir(root)
    code, out, err = run(capsys, "deploy")
    assert code == 0, err
    assert "заведён сервис bot" in out and "стоит на staging" in out
    assert "https://bot-staging.apps.example.ru/" in out
    names = fake_api.archive_names(state.archives[("bot", 1)])
    assert names == [".loomaignore", "README.md", "app.py", "looma.yaml"]
    # Второй релиз — сразу на prod.
    code, out, _ = run(capsys, "deploy", "--promote")
    assert code == 0 and "переведён на prod" in out
    code, out, _ = run(capsys, "status")
    assert "prod: релиз 2 · ok" in out and "staging: релиз 2" in out and "web#0" in out
    code, out, _ = run(capsys, "rollback")
    assert code == 0 and "prod — снова релиз 1" in out
    code, _out, err = run(capsys, "rollback")
    assert code == 1 and "откатывать некуда" in err
    code, out, _ = run(capsys, "releases")
    assert out.splitlines()[0].split()[:2] == ["2", "ready"]
    code, out, _ = run(capsys, "apps")
    assert out.startswith("bot")


def test_a_failed_build_shows_the_log_tail(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    monkeypatch.chdir(tmp_path)
    run(capsys, "init", "bot")
    state.fail_build = True
    code, out, err = run(capsys, "deploy", "bot")
    assert code == 1 and "сборка не удалась" in err
    assert "ERROR: pip failed" in out


def test_secrets_logs_destroy(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    monkeypatch.chdir(tmp_path)
    run(capsys, "init", "bot")
    run(capsys, "deploy", "bot", "--promote")
    code, out, _ = run(capsys, "secrets", "--app", "bot", "set", "TG_TOKEN=1:abc", "MODE=prod")
    assert code == 0 and "TG_TOKEN" in out and "MODE" in out
    assert state.app("bot")["secrets"] == {"TG_TOKEN": "1:abc", "MODE": "prod"}
    run(capsys, "secrets", "--app", "bot", "unset", "MODE")
    code, out, _ = run(capsys, "secrets", "--app", "bot")
    assert "TG_TOKEN" in out and "MODE" not in out
    code, out, _ = run(capsys, "logs", "--app", "bot")
    assert code == 0 and "строка 1" in out
    code, out, _ = run(capsys, "restart", "web", "--app", "bot", "--env", "staging")
    assert code == 0 and "спит" in out
    code, _out, err = run(capsys, "destroy", "--app", "bot")
    assert code == 1 and "--yes" in err
    code, out, _ = run(capsys, "destroy", "--app", "bot", "--yes")
    assert code == 0 and state.app("bot") is None


def test_sdk_refreshes_the_vendored_copy(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _out, err = run(capsys, "sdk")
    assert code == 1 and "looma.yaml" in err
    code, _out, err = run(capsys, "init", "x", "--template", "")
    assert code == 1 and "шаблона" in err and not (tmp_path / "x").exists()
    run(capsys, "init", "studio", "--template", "image-studio")
    sdk = tmp_path / "studio" / "loomafloat"
    (sdk / "http.py").write_text("# старая копия\n")
    (sdk / "mine.py").write_text("# своё — не трогать\n")
    code, out, _ = run(capsys, "sdk", "studio")
    assert code == 0 and "SDK loomafloat" in out
    assert (sdk / "http.py").read_text() == (Path(cli.__file__).with_name("http.py")).read_text()
    assert (sdk / "mine.py").read_text() == "# своё — не трогать\n"


def test_deploy_puts_the_sdk_in_when_the_code_imports_it(api, capsys, tmp_path, monkeypatch):
    """Папку SDK удалили (или забыли `looma sdk`): сборка прошла бы, а процесс
    упал бы на import. Деплой кладёт копию сам."""
    base, state = api
    login(base, capsys)
    root = tmp_path / "board"
    root.mkdir()
    (root / "looma.yaml").write_text("name: board\nruntime: {kind: python}\n"
                                      "processes: {web: {cmd: x, port: 8080}}\n")
    (root / "app.py").write_text("import os\nfrom loomafloat import client_of\n")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / "node_modules" / "x" / "skip.py").write_text("import loomafloat\n")
    monkeypatch.chdir(root)
    code, out, err = run(capsys, "deploy")
    assert code == 0, err
    assert "копии SDK в проекте не было" in out
    names = fake_api.archive_names(state.archives[("board", 1)])
    assert "loomafloat/__init__.py" in names and "loomafloat/client.py" in names
    # Копия уже есть — второй деплой молчит.
    code, out, _ = run(capsys, "deploy")
    assert code == 0 and "копии SDK" not in out


def test_deploy_leaves_projects_without_the_sdk_alone(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    for name, files in (("plain", {"app.py": "print('loomafloat в строке — не import')\n"}),
                        ("pinned", {"app.py": "import loomafloat\n",
                                    "requirements.txt": "loomafloat @ git+https://example.ru/sdk\n"}),
                        ("hidden", {"app.py": "print(1)\n", "data/old.py": "import loomafloat\n",
                                    ".loomaignore": "data/\n"})):
        root = tmp_path / name
        root.mkdir()
        (root / "looma.yaml").write_text(f"name: {name}\nruntime: {{kind: python}}\n"
                                          "processes: {web: {cmd: x, port: 8080}}\n")
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        code, out, err = run(capsys, "deploy", str(root))
        assert code == 0, err
        assert not (root / "loomafloat").exists() and "копии SDK" not in out, name


def test_pack_rules_and_manifest_name(tmp_path):
    with pytest.raises(PackError, match="looma init"):
        pack(tmp_path)
    (tmp_path / "looma.yaml").write_text("# x\nname: my-app  # имя\nruntime: {kind: python}\n")
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / "data" / "raw" / "big.bin").write_text("x")
    (tmp_path / "data" / "keep.txt").write_text("x")
    (tmp_path / ".loomaignore").write_text("data/raw\n")
    (tmp_path / "link").symlink_to("/etc/hosts")
    _data, files, skipped = pack(tmp_path)
    assert "data/keep.txt" in files and not any(f.startswith("data/raw") for f in files)
    assert skipped == ["link"]
    assert manifest_name(tmp_path) == "my-app"


def test_sdk_calls_models_and_downloads_without_the_key(api):
    base, state = api
    looma = Looma(api_url=base + "/v1", key=fake_api.KEY)       # как LOOMA_API_URL в процессе
    assert looma.base == base
    assert looma.chat("chat-model", "привет") == "эхо: привет"
    assert looma.image("img", "кот", n=2) == [fake_api.PNG, fake_api.PNG]
    # Векторы идут по порядку строк, даже если сервер прислал их в другом.
    assert looma.embed("emb", ["ab", "abcd"], input_type="document") == [[2.0, 0.5],
                                                                         [4.0, 0.5]]
    assert looma.embed("emb", "abc") == [[3.0, 0.5]]
    sent = state.embeds[0]
    assert sent["encoding_format"] == "base64" and sent["input_type"] == "document"
    assert looma.rerank("rr", "вопрос", ["a", "ccc", "bb"], top_n=2) == [
        {"index": 1, "relevance_score": 1.0}, {"index": 2, "relevance_score": 0.5}]
    assert state.reranks[0]["return_documents"] is False
    assert looma.image("img", "шляпа", images=[b"\x89PNG..."]) == [fake_api.PNG]
    edit = json.loads([c for c in state.calls if c[1] == "/v1/images/edits"][-1][3])
    assert edit["images"][0].startswith("data:image/png;base64,")
    video = looma.video("vid", "лиса", quality="fast")
    assert video.wait().status == "completed"
    assert video.content() == fake_api.MP4
    # За файлом в хранилище — без ключа API (подставное «хранилище» его не берёт).
    file_call = [c for c in state.calls if c[1] == "/files/video.mp4"][-1]
    assert file_call[2] == ""
    with pytest.raises(LoomaError) as caught:
        Looma(api_url=base, key="looma_sk_wrong").models()
    assert caught.value.status == 401 and "войти" in caught.value.message


def test_environment_beats_the_file(api, monkeypatch, capsys):
    base, _state = api
    login(base, capsys)
    monkeypatch.setenv("LOOMA_APP_TOKEN", "looma_app_x")
    monkeypatch.setenv("LOOMA_API_URL", "https://api.example.ru/v1")
    looma = Looma()
    assert (looma.base, looma.key) == ("https://api.example.ru", "looma_app_x")
    assert config.load()["key"] == fake_api.KEY


def test_a_write_is_retried_only_when_it_never_left(monkeypatch):
    """Запись повторяется, только если соединение не установилось: тогда
    сервер её не видел. Обрыв после отправки — не повторяется."""
    import socket
    import urllib.error
    import urllib.request

    from loomafloat import http

    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    opened = []
    real = urllib.request.build_opener

    def counting(*handlers):
        opener = real(*handlers)
        original = opener.open

        def open_(call, *a, **kw):
            opened.append(call.get_method())
            return original(call, *a, **kw)
        opener.open = open_
        return opener
    monkeypatch.setattr(urllib.request, "build_opener", counting)
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    with pytest.raises(LoomaError) as refused:
        http.send_json("POST", f"http://127.0.0.1:{port}/v1/apps", {"name": "x"})
    assert refused.value.status == 0 and opened == ["POST"] * (http.RETRIES_NETWORK + 1)
    assert http._not_sent(urllib.error.URLError(TimeoutError("The handshake operation timed out")))
    assert not http._not_sent(urllib.error.URLError(TimeoutError("timed out")))
    assert not http._not_sent(TimeoutError("read timed out"))


def test_publish_listing_and_unpublish(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    monkeypatch.chdir(tmp_path)
    run(capsys, "init", "bot", "--template", "hello")
    monkeypatch.chdir(tmp_path / "bot")
    run(capsys, "deploy")
    code, _out, err = run(capsys, "publish")
    assert code == 1 and "looma promote" in err          # на prod ещё ничего
    run(capsys, "promote")
    code, out, err = run(capsys, "publish")
    assert code == 0, err
    assert "отправлены на модерацию, версия 1" in out and "looma listing" in out
    listing = state.apps[state.app("bot")["id"]]["listing"]
    listing["versions"][0].update(status="rejected", reason="иконка не видна")
    listing["pending"] = None
    code, out, _ = run(capsys, "listing")
    assert code == 0 and "версия 1 — отказ: иконка не видна" in out
    listing["versions"][0].update(status="approved", reason="")
    listing.update(live_version=1, url="https://market.example.ru/app/bot")
    code, out, _ = run(capsys, "listing")
    assert "в каталоге, версия 1 — https://market.example.ru/app/bot" in out
    code, out, _ = run(capsys, "unpublish")
    assert code == 0 and "снята автором" in out and "looma publish" in out


def test_stats_reads_like_a_report(api, capsys, tmp_path, monkeypatch):
    base, _state = api
    login(base, capsys)
    monkeypatch.chdir(tmp_path)
    run(capsys, "init", "bot", "--template", "hello")
    monkeypatch.chdir(tmp_path / "bot")
    run(capsys, "deploy")
    code, out, err = run(capsys, "stats", "--days", "7")
    assert code == 0, err
    assert "bot — за 7 дн." in out
    assert "запросы: 6 (веб 3, API 1, MCP 1, Telegram 1)" in out
    assert "клиенты: всего 2, новых 1, заходили 2" in out
    assert "Ответ 3 (клиентов: 2)" in out and "chat: вызовы 1, токены 1000/500, 0,30 ₽" in out
    assert "Qwen-Image-2.1: вызовы 1, изображения 2, —" in out
    assert "себестоимость: 0,30 ₽" in out and "нет в прайсе (не посчитаны): Qwen-Image-2.1" in out
    assert "looma dev: вызовы моделей 3" in out and "cl_bob" in out


# ------------------------------------------------- свои модели в каталоге
def test_own_model_from_weights_to_publication(api, capsys):
    base, state = api
    login(base, capsys)
    code, out, _err = run(capsys, "model")
    assert code == 0 and "своих моделей нет" in out

    code, _out, err = run(capsys, "model", "add", "qwen-legal", "--repo", "anna/qwen-legal")
    assert code == 1 and "имя автора" in err, "без имени автора модель не завести"
    code, out, _err = run(capsys, "model", "name", "anna")
    assert code == 0 and "anna/<имя>" in out and state.handle == "anna"
    code, _out, err = run(capsys, "model", "name", "other")
    assert code == 1 and "уже задано: anna" in err

    code, out, _err = run(capsys, "model", "check", "--repo", "anna/qwen-legal", "--set",
                          "stages=2")
    assert code == 0 and "веса проходят" in out and "8.0 ГиБ" in out and "на 2 узлах" in out
    assert state.model_checks[-1] == {"repo": "anna/qwen-legal", "stages": 2}
    code, _out, err = run(capsys, "model", "check", "--repo", "anna/pickle-only")
    assert code == 1 and "safetensors" in err

    code, out, _err = run(capsys, "model", "add", "qwen-legal", "--repo", "anna/qwen-legal",
                          "--engine", "vllm", "--set", "max_model_len=8192", "--summary",
                          "Юридический ассистент", "--always")
    assert code == 0 and "anna/qwen-legal: веса" in out and "looma model publish" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/services")][-1][3])
    assert sent == {"name": "qwen-legal", "summary": "Юридический ассистент", "min_replicas": 1,
                    "max_replicas": 1, "max_model_len": 8192, "repo": "anna/qwen-legal",
                    "engine": "vllm"}

    code, out, _err = run(capsys, "model", "set", "qwen-legal", "--price", "in=5", "--price",
                          "out=12,5", "--sleep")
    assert code == 0 and "вход 5,00 ₽, выход 12,50 ₽ за 1M токенов" in out
    assert state.entries[0]["price"] == {"price_in": 500, "price_out": 1250}
    assert state.entries[0]["min_replicas"] == 0
    code, _out, err = run(capsys, "model", "set", "qwen-legal", "--price", "tokens=5")
    assert code == 1 and "единицы: in, out, image, second" in err
    code, _out, err = run(capsys, "model", "set", "qwen-legal")
    assert code == 1 and "что поменять" in err

    code, out, _err = run(capsys, "model", "publish", "anna/qwen-legal")
    assert code == 0 and "отправлена на проверку" in out and "на проверке" in out
    code, out, _err = run(capsys, "model", "list")
    assert "anna/qwen-legal" in out and "веса" in out and "на проверке" in out
    code, out, _err = run(capsys, "model", "unpublish", "qwen-legal")
    assert code == 0 and "видите только вы" in out

    code, _out, err = run(capsys, "model", "delete", "qwen-legal")
    assert code == 1 and "--yes" in err and state.entries
    code, out, _err = run(capsys, "model", "delete", "qwen-legal", "--yes")
    assert code == 0 and "удалена" in out and state.entries == []
    code, _out, err = run(capsys, "model", "show", "qwen-legal")
    assert code == 1 and "у вас нет" in err


def test_preset_over_a_catalog_model(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    preset = {"system": "Ты переводчик договоров.", "template": "Переведи:\n{text}",
              "inputs": [{"name": "text", "title": "Текст договора"}]}
    path = tmp_path / "preset.json"
    path.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")

    code, _out, err = run(capsys, "model", "add", "translate", "--base", "chat-model")
    assert code == 1 and "Одно из трёх" in err
    code, _out, err = run(capsys, "model", "add", "translate", "--base", "chat-model",
                          "--preset", str(path), "--always")
    assert code == 1 and "нет своих реплик" in err
    code, _out, err = run(capsys, "model", "add", "translate", "--base", "chat-model",
                          "--preset", str(tmp_path / "нет.json"))
    assert code == 1 and "не прочитать" in err

    code, out, _err = run(capsys, "model", "add", "translate", "--base", "chat-model",
                          "--preset", str(path), "--summary", "Перевод договоров")
    assert code == 0 and "anna/translate: настройка" in out
    assert "работает на: chat-model (своих реплик нет)" in out and "без надбавки" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/services")][-1][3])
    assert sent == {"name": "translate", "summary": "Перевод договоров", "base": "chat-model",
                    "preset": preset}

    path.write_text(json.dumps({"system": "Ты редактор."}), encoding="utf-8")
    code, out, _err = run(capsys, "model", "set", "translate", "--preset", str(path), "--price",
                          "out=50")
    assert code == 0 and "надбавка: выход 50,00 ₽ за 1M токенов" in out
    assert state.entries[0]["preset"] == {"system": "Ты редактор."}
    code, out, _err = run(capsys, "model", "show", "translate")
    assert code == 0 and '"system": "Ты редактор."' in out
    code, out, _err = run(capsys, "model", "list")
    assert "настройка" in out and "поверх chat-model" in out

    # SDK: настройку со своим входом зовут полями вместо сообщений.
    looma = Looma(api_url=base, key=fake_api.KEY)
    assert looma.chat("anna/translate", input={"text": "Договор"}) == 'поля: {"text": "Договор"}'
    assert state.chats[-1] == {"model": "anna/translate", "input": {"text": "Договор"}}
    assert looma.chat("chat-model", "привет", temperature=0.2) == "эхо: привет"
    assert "".join(looma.chat_stream("anna/translate", input={"text": "x"})).startswith("поля")


def test_model_with_an_adapter(api, capsys):
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    code, out, _err = run(capsys, "model", "adapters")
    assert code == 0 and "legal" in out and "Qwen/Qwen3-0.6B" in out
    code, out, _err = run(capsys, "model", "add", "qwen-legal", "--base", "chat-model",
                          "--adapter", "legal")
    assert code == 0 and "anna/qwen-legal: адаптер" in out and "адаптер: adp-1" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/services")][-1][3])
    assert sent == {"name": "qwen-legal", "min_replicas": 0, "max_replicas": 1,
                    "base": "chat-model", "adapter": "legal"}



def test_own_engine_from_repository_to_a_model(api, capsys):
    base, state = api
    login(base, capsys)
    commit = "c" * 40
    code, out, _err = run(capsys, "engine")
    assert code == 0 and "своих движков нет" in out
    code, _out, err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo")
    assert code == 1 and "--commit" in err
    code, _out, err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo",
                          "--commit", commit)
    assert code == 1 and "looma model name" in err, "сначала имя автора"
    state.handle = "anna"
    code, out, err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo",
                         "--commit", commit, "--path", "engines/echo")
    assert code == 0, err
    assert "anna/echo: движок" in out and "версия 1: ждёт проверки, коммит cccccccccccc" in out
    assert "looma model add <имя> --engine anna/echo" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/layers")][-1][3])
    assert sent == {"repo": "https://github.com/anna/echo", "commit": commit, "kind": "engine",
                    "path": "engines/echo"}

    code, _out, err = run(capsys, "engine", "open", "echo")
    assert code == 1 and "нет одобренной версии" in err
    state.layers[0].update(approved=1)
    state.layers[0]["versions"][0].update(status="approved")
    code, out, _err = run(capsys, "engine", "open", "echo")
    assert code == 0 and "открыт" in out and "открыт авторам" in out
    code, out, _err = run(capsys, "engine", "show", "anna/echo")
    assert code == 0 and "версия 1: одобрена" in out
    code, _out, err = run(capsys, "engine", "show", "nope")
    assert code == 1 and "looma engine list" in err

    code, out, _err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo",
                          "--commit", commit, "--extension")
    assert code == 0 and "anna/upper: расширение" in out and "для движка anna/echo" in out

    # Модель на своём движке: весов у неё может не быть.
    code, out, _err = run(capsys, "model", "check", "--engine", "anna/echo")
    assert code == 0 and "целиком на одном узле" in out and "веса проходят" not in out
    code, out, err = run(capsys, "model", "add", "parrot", "--engine", "anna/echo",
                         "--set", "repeat=2")
    assert code == 0, err
    assert "anna/parrot: движок" in out and "движок: anna/echo" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/services")][-1][3])
    assert sent == {"name": "parrot", "min_replicas": 0, "max_replicas": 1, "repeat": 2,
                    "engine": "anna/echo"}


def test_model_on_own_nodes_and_engine_fee(api, capsys):
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    code, out, err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo",
                         "--commit", "c" * 40, "--fee", "in=1", "--fee", "out=2.5")
    assert code == 0, err
    assert "доля: вход 1,00 ₽, выход 2,50 ₽ за 1M токенов" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/layers")][-1][3])
    assert sent["fee"] == {"price_in": 100, "price_out": 250}
    code, _out, err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo",
                          "--commit", "c" * 40, "--fee", "image=5")
    assert code == 1 and "in и out" in err

    code, out, err = run(capsys, "model", "add", "parrot", "--engine", "anna/echo", "--own")
    assert code == 0, err
    assert "узлы: только свои" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/services")][-1][3])
    assert sent["contours"] == ["own"]
    code, out, _err = run(capsys, "model", "set", "parrot", "--network")
    assert code == 0 and "узлы: только свои" not in out
    sent = json.loads([c for c in state.calls if c[0] == "PATCH"][-1][3])
    assert sent == {"contours": None}
    code, out, _err = run(capsys, "model", "set", "parrot", "--own")
    assert code == 0 and "узлы: только свои" in out


def test_image_engine_states_in_the_list(api, capsys):
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    state.layers = [{"ref": "anna/box", "kind": "engine", "title": "Образ", "public": False,
                     "approved": None, "versions": [
                         {"number": 2, "status": "pending", "note": "", "form": "image",
                          "build": "building", "source": {"commit": "e" * 40}},
                         {"number": 1, "status": "rejected", "form": "image", "build": "failed",
                          "note": "сборка не удалась: exited with code 1",
                          "log": "step 3/5\npip: no such package", "source": {"commit": "d" * 40}}]}]
    code, out, _err = run(capsys, "engine")
    assert code == 0
    assert "версия 2: образ собирается" in out and "версия 1: образ не собран" in out
    assert "сборка: сборка не удалась: exited with code 1" in out
    assert "pip: no such package" in out and "ревью" not in out


def test_цена_и_доля_за_вызов():
    """Общая единица: модель со своим API считается вызовами, а не токенами."""
    from loomafloat import ops

    assert ops.price_of(["call=1.5"]) == {"price_call": 150}
    assert ops.fee_of(["call=0,2", "in=1"]) == {"price_call": 20, "price_in": 100}
    assert ops.fee_text({"price_call": 20}) == "0,20 ₽ за вызов"
    assert "за вызов" in ops.fee_text({"price_call": 20, "price_out": 300}) \
        and "за 1M токенов" in ops.fee_text({"price_call": 20, "price_out": 300})
    assert ops.entry_price({"price": {"price_call": 150}}) == "за вызов 1,50 ₽"
    with pytest.raises(ops.EntryError, match="call"):
        ops.fee_of(["image=1"])
