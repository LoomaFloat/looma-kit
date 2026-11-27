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
    assert code == 1 and "Одно из четырёх" in err
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

    # Перевести модель на другую версию движка: вместе с версией можно
    # поменять и поля движка; пустое значение убирает поле.
    code, _out, err = run(capsys, "model", "set", "parrot", "--engine-version", "latest")
    assert code == 0, err
    assert json.loads([c for c in state.calls if c[0] == "PATCH"][-1][3]) == \
        {"engine_version": "latest"}
    code, _out, err = run(capsys, "model", "set", "parrot", "--engine-version", "3", "--set",
                          "repeat=2", "--set", "prefix=")
    assert code == 0, err
    assert json.loads([c for c in state.calls if c[0] == "PATCH"][-1][3]) == \
        {"engine_version": 3, "fields": {"repeat": 2, "prefix": None}}
    code, _out, err = run(capsys, "model", "set", "parrot", "--engine-version", "новая")
    assert code == 1 and "номер версии или latest" in err


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


def test_own_engine_from_a_folder_on_own_nodes(api, capsys, tmp_path):
    """Свой движок из папки на свои узлы, без ревью и без GitHub."""
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    code, _out, err = run(capsys, "engine", "try", str(tmp_path))
    assert code == 1 and "looma-engine.yaml" in err, "не папка движка"
    folder = tmp_path / "solver"
    (folder / "solver" / "__pycache__").mkdir(parents=True)
    (folder / "looma-engine.yaml").write_text("name: echo\n")
    (folder / "solver" / "serve.py").write_text("print('ok')\n")
    (folder / "solver" / "__pycache__" / "serve.cpython-312.pyc").write_bytes(b"\x00")
    (folder / ".env").write_text("TOKEN=секрет\n")
    code, out, err = run(capsys, "engine", "try", str(folder))
    assert code == 0, err
    assert "версия 1: без ревью, работает только на ваших узлах, из папки" in out
    assert "looma model add <имя> --engine anna/echo" in out
    names = state.engine_archives[-1]
    assert sorted(names) == ["engine/looma-engine.yaml", "engine/solver/serve.py"], \
        "кэш и секреты в архив не едут"
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/layers")][-1][3])
    assert sent["own"] is True and sent["kind"] == "engine" and "repo" not in sent
    # Из репозитория без ревью — флагом.
    code, out, err = run(capsys, "engine", "submit", "--repo", "https://github.com/anna/echo",
                         "--commit", "c" * 40, "--own")
    assert code == 0 and "без ревью: работает только на ваших узлах" in out, err


def test_nodes_from_the_terminal(api, capsys):
    base, state = api
    login(base, capsys)
    code, out, _err = run(capsys, "node")
    assert code == 0 and "своих узлов нет" in out
    code, out, err = run(capsys, "node", "connect", "--label", "Мастерская")
    assert code == 0, err
    assert "docker run -d --gpus all gihpee/looma-agent:1.0.0 --key looma_secretkey1" in out
    assert "показывается один раз" in out
    code, out, _err = run(capsys, "node", "list")
    assert "home-1 «Мастерская»: ждёт подключения" in out and "работает только на вас" in out
    code, out, err = run(capsys, "node", "rename", "home-1", "--label", "Стенд")
    assert code == 0 and "home-1 «Стенд»" in out, err
    code, _out, err = run(capsys, "node", "disconnect", "home-1")
    assert code == 1 and "--yes" in err
    code, out, _err = run(capsys, "node", "disconnect", "Стенд", "--yes")
    assert code == 0 and "отключён" in out and state.nodes == []
    code, _out, err = run(capsys, "node", "show", "home-9")
    assert code == 1 and "looma node list" in err


def test_call_own_api_model_and_its_job(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    code, out, err = run(capsys, "call", "anna/solver", "--path", "/tasks?fast=1",
                         "--data", '{"x": 1}')
    assert code == 0, err
    assert "ответ 200, единиц 3" in out and '"path": "/tasks"' in out
    method, model, rest, prefer, body = state.runs[-1]
    assert (method, model, rest, prefer, json.loads(body)) == \
        ("POST", "anna/solver", "/tasks", None, {"x": 1})
    code, out, err = run(capsys, "call", "anna/solver", "--path", "/render", "--async")
    assert code == 0, err
    assert "run_1: queued" in out and "looma job run_1 --wait" in out
    assert state.runs[-1][3] == "respond-async"
    target = tmp_path / "result.bin"
    code, out, err = run(capsys, "job", "run_1", "--out", str(target))
    assert code == 0, err
    assert "run_1: succeeded" in out and target.read_bytes() == b"RESULT"
    code, out, _err = run(capsys, "income", "--period", "week")
    assert "модель anna/solver: вызовов 4" in out and "выплат пока нет" in out


def test_model_on_a_named_own_node(api, capsys):
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    code, _out, err = run(capsys, "model", "add", "parrot", "--repo", "Qwen/Qwen3-0.6B",
                          "--node", "home-1")
    assert code == 1 and "движке автора" in err
    code, _out, err = run(capsys, "model", "add", "parrot", "--engine", "anna/echo",
                          "--node", "home-1")
    assert code == 0, err
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/services")][-1][3])
    assert sent["own_node"] == "home-1" and sent["engine"] == "anna/echo"


def test_model_logs(api, capsys):
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    run(capsys, "model", "add", "parrot", "--engine", "anna/echo")
    code, out, err = run(capsys, "model", "logs", "parrot", "--tail", "50")
    assert code == 0, err
    assert "реплика 0000abcd, ранг 0 на home-1: failed" in out and "No module named" in out


def test_engine_from_a_folder_to_review(api, capsys, tmp_path):
    """Движок на ревью — папкой из рабочей среды, как сервис; GitHub по желанию."""
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    folder = tmp_path / "solver"
    (folder / "solver").mkdir(parents=True)
    (folder / "looma-engine.yaml").write_text("name: echo\n")
    (folder / "solver" / "serve.py").write_text("print('ok')\n")
    code, _out, err = run(capsys, "engine", "submit", str(folder), "--repo",
                          "https://github.com/anna/echo", "--commit", "c" * 40)
    assert code == 1 and "вместе не нужно" in err
    code, _out, err = run(capsys, "engine", "submit", "--commit", "c" * 40)
    assert code == 1 and "нужны оба" in err
    code, out, err = run(capsys, "engine", "submit", str(folder), "--origin",
                         "https://example.com/anna/echo", "--fee", "call=0.2")
    assert code == 0, err
    assert "папка solver: 2 файлов" in out and "ждёт проверки площадки" in out
    assert "версия 1: ждёт проверки, из папки abcdef0123456789" in out
    sent = json.loads([c for c in state.calls if c[:2] == ("POST", "/api/layers")][-1][3])
    assert "own" not in sent and sent["origin"] == "https://example.com/anna/echo"
    assert sent["fee"] == {"price_call": 20} and "repo" not in sent
    assert sorted(state.engine_archives[-1]) == ["engine/looma-engine.yaml",
                                                 "engine/solver/serve.py"]


def test_reading_the_code_of_an_engine(api, capsys):
    """Код одобренного движка видит любой, кто вошёл: например, владелец узла."""
    base, _state = api
    login(base, capsys)
    code, out, err = run(capsys, "engine", "code", "bob/solver")
    assert code == 0, err
    assert "bob/solver: версия 2" in out and "python -m solver.serve" in out
    assert "ссылка автора https://example.com/bob/solver (площадка её не проверяет)" in out
    assert "solver/serve.py (21 Б)" in out
    code, out, _err = run(capsys, "engine", "code", "bob/solver", "--file", "solver/serve.py")
    assert code == 0 and out == "print('решаю')\n"
    code, _out, err = run(capsys, "engine", "code", "echo")
    assert code == 1 and "автор/движок" in err


GRAPH = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
         "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "кот", "clip": ["4", 1]}},
         "9": {"class_type": "SaveImage", "inputs": {"images": ["6", 0]}}}


def test_comfyui_graph_from_inspect_to_a_model(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    run(capsys, "model", "name", "anna")
    graph = tmp_path / "workflow_api.json"
    graph.write_text(json.dumps(GRAPH), encoding="utf-8")
    described = tmp_path / "portrait.comfy.yaml"
    code, out, err = run(capsys, "comfy", "inspect", str(graph), "--write", str(described))
    assert code == 0, err
    assert "выход: узел 9 (SaveImage, изображение)" in out
    assert "вес: checkpoints/sdxl.safetensors" in out and "род модели: image" in out
    assert "Допишите repo у sdxl.safetensors, проверьте входы" in out
    text = described.read_text(encoding="utf-8")
    assert 'graph: "workflow_api.json"' in text and 'output: {node: "9"}' in text
    assert '  prompt: {node: "6", field: "text", type: "text"}' in text
    assert '  - {folder: "checkpoints", repo: "", file: "sdxl.safetensors"}' in text
    code, _out, err = run(capsys, "comfy", "inspect", str(graph), "--write", str(described))
    assert code == 1 and "не перезаписывает" in err

    code, out, err = run(capsys, "model", "check", "--from", str(described))
    assert code == 0, err
    assert "граф проходит: ComfyUI 0.46.0, род image" in out and "встанет в сети" in out
    sent = state.model_checks[-1]
    assert sent["engine"] == "comfyui" and sent["graph"] == GRAPH
    assert sent["description"] == text

    code, out, err = run(capsys, "model", "add", "portrait", "--from", str(described))
    assert code == 0, err
    made = state.entries[-1]
    assert made["label"] == "anna/portrait" and made["recipe"]["graph"] == GRAPH
    assert made["recipe"]["engine"] == "comfyui" and "description" in made["recipe"]
    code, _out, err = run(capsys, "model", "add", "x", "--from", str(described),
                          "--repo", "anna/w")
    assert code == 1 and "описание графа" in err

    changed = {**GRAPH, "6": {**GRAPH["6"], "inputs": {**GRAPH["6"]["inputs"], "text": "пёс"}}}
    graph.write_text(json.dumps(changed), encoding="utf-8")
    code, out, err = run(capsys, "model", "set", "portrait", "--from", str(described))
    assert code == 0, err
    assert state.regraphs[-1]["graph"] == changed and made["recipe"]["revision"] == 1

    editor = tmp_path / "editor.json"
    editor.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
    code, _out, err = run(capsys, "comfy", "inspect", str(editor))
    assert code == 1 and "Export (API)" in err
    lost = tmp_path / "lost.comfy.yaml"
    lost.write_text("output: {node: 9}\n", encoding="utf-8")
    code, _out, err = run(capsys, "model", "add", "lost", "--from", str(lost))
    assert code == 1 and "нет строки graph" in err


def test_comfyui_node_sets(api, capsys, tmp_path):
    base, _state = api
    login(base, capsys)
    code, out, err = run(capsys, "comfy", "sets")
    assert code == 0, err
    assert "looma-sets/kjnodes, версия 2: KJNodes" in out and "узлов 2" in out
    graph = tmp_path / "workflow_api.json"
    graph.write_text(json.dumps({**GRAPH, "11": {"class_type": "ImageResizeKJv2",
                                                 "inputs": {"image": ["8", 0]}}}),
                     encoding="utf-8")
    described = tmp_path / "kj.comfy.yaml"
    code, out, err = run(capsys, "comfy", "inspect", str(graph), "--write", str(described))
    assert code == 0, err
    assert "набор узлов: looma-sets/kjnodes, версия 2 (узлы графа: ImageResizeKJv2)" in out
    assert 'extensions: ["looma-sets/kjnodes"]' in described.read_text(encoding="utf-8")
    code, _out, err = run(capsys, "comfy", "inspect")
    assert code == 1 and "нужен граф" in err


def test_comfyui_graph_runs_without_a_model(api, capsys, tmp_path, monkeypatch):
    """Граф прогоняется на площадке без модели: веса из описания, входной файл
    рядом с графом едет сам, ход печатается, файлы прогона сохраняются."""
    base, state = api
    login(base, capsys)
    monkeypatch.setattr("loomafloat.client.time.sleep", lambda s: None)
    graph = tmp_path / "workflow_api.json"
    graph.write_text(json.dumps({**GRAPH, "12": {"class_type": "LoadImage",
                                                 "inputs": {"image": "cat.png"}}}),
                     encoding="utf-8")
    code, _out, err = run(capsys, "comfy", "run", str(graph))
    assert code == 1 and "sdxl.safetensors" in err and "weights" in err
    described = tmp_path / "portrait.comfy.yaml"
    described.write_text("graph: workflow_api.json\nweights:\n  - {folder: checkpoints, "
                         "repo: stabilityai/sdxl, file: sdxl.safetensors}\n", encoding="utf-8")
    code, _out, err = run(capsys, "comfy", "run", str(graph), "--weights", str(described))
    assert code == 1 and "cat.png" in err, "файла рядом нет — площадка называет, чего не хватает"
    (tmp_path / "cat.png").write_bytes(b"\x89PNG cat")
    out_dir = tmp_path / "out"
    code, out, err = run(capsys, "comfy", "run", str(described), "--out", str(out_dir))
    assert code == 0, err
    sent = state.run_requests[-1]
    assert sent["description"].startswith("graph: workflow_api.json")
    assert list(sent["files"]) == ["cat.png"] and "входные файлы: cat.png" in out
    assert "узел 3, шаг 5 из 20" in out and "готово за 4 с: файлов 2" in out
    assert out.count("в очереди") == 1, "та же строка хода второй раз не печатается"
    saved = sorted(p.name for p in out_dir.iterdir())
    assert saved == ["0_9_ComfyUI_0.png", "1_15_prev.png"]
    assert (out_dir / "0_9_ComfyUI_0.png").read_bytes().startswith(b"PNG:/files/run_")
    assert "узел 14, text: [\"готово\"]" in out
    code, out, err = run(capsys, "comfy", "runs")
    assert code == 0 and "готово" in out
    run_id = out.split()[0]
    code, out, err = run(capsys, "comfy", "runs", run_id)
    assert code == 0 and "файл 1: узел 15, prev.png (просмотр, 1 КБ)" in out
    code, out, err = run(capsys, "comfy", "run", str(described), "--no-wait")
    assert code == 0 and "looma comfy runs run_" in out
    queued = __import__("re").search(r"run_\d+", out).group(0)
    code, out, err = run(capsys, "comfy", "cancel", queued)
    assert code == 0 and "снято" in out


def test_editor_weights_feed_runs_without_weights(api, capsys, tmp_path, monkeypatch):
    """Веса редактора: файл добавляется с папкой, виден в списке вместе с весами
    площадки, прогон без --weights его находит, убирается по имени."""
    base, state = api
    login(base, capsys)
    monkeypatch.setattr("loomafloat.client.time.sleep", lambda s: None)
    code, out, _err = run(capsys, "comfy", "weights")
    assert code == 0 and "свои веса: 0 из 100" in out and "looma/vae/sdxl_vae.safetensors" in out
    code, _out, err = run(capsys, "comfy", "weights", "add", "stabilityai/sdxl/sdxl.safetensors")
    assert code == 1 and "--folder" in err
    code, out, _err = run(capsys, "comfy", "weights", "add", "stabilityai/sdxl/sdxl.safetensors",
                          "--folder", "checkpoints")
    assert code == 0 and "stabilityai/sdxl/sdxl.safetensors добавлен: checkpoints" in out
    code, out, _err = run(capsys, "comfy", "weights", "add", "stabilityai/sdxl/sdxl.safetensors",
                          "--folder", "checkpoints", "--revision", "e" * 40)
    assert code == 0 and "закреплён заново" in out and "коммит eeeeeeee" in out
    code, out, _err = run(capsys, "comfy", "weights")
    assert "свои веса: 1 из 100" in out and "1  checkpoints  stabilityai/sdxl/sdxl.safetensors" in out
    graph = tmp_path / "workflow_api.json"
    graph.write_text(json.dumps(GRAPH), encoding="utf-8")
    code, out, err = run(capsys, "comfy", "run", str(graph), "--out", str(tmp_path / "out"))
    assert code == 0, err
    assert "weights" not in state.run_requests[-1] and "готово" in out
    code, out, _err = run(capsys, "comfy", "weights", "rm", "stabilityai/sdxl/sdxl.safetensors")
    assert code == 0 and "убран" in out
    code, _out, err = run(capsys, "comfy", "run", str(graph))
    assert code == 1 and "ни в весах редактора" in err


def test_successful_run_becomes_a_model_draft(api, capsys, tmp_path, monkeypatch):
    """`looma comfy inspect run_…`: граф прогона с площадки ложится рядом с
    черновиком описания, описание ссылается на него."""
    base, state = api
    login(base, capsys)
    monkeypatch.setattr("loomafloat.client.time.sleep", lambda s: None)
    run(capsys, "comfy", "weights", "add", "stabilityai/sdxl/sdxl.safetensors", "--folder", "checkpoints")
    graph = tmp_path / "workflow_api.json"
    graph.write_text(json.dumps(GRAPH), encoding="utf-8")
    code, out, err = run(capsys, "comfy", "run", str(graph), "--no-wait")
    assert code == 0, err
    run_id = __import__("re").search(r"run_\d+", out).group(0)
    described = tmp_path / "drafts" / "fox.comfy.yaml"
    described.parent.mkdir()
    code, out, err = run(capsys, "comfy", "inspect", run_id, "--write", str(described))
    assert code == 0, err
    saved = described.parent / f"{run_id}.json"
    assert json.loads(saved.read_text(encoding="utf-8")) == GRAPH
    assert f'graph: "{run_id}.json"' in described.read_text(encoding="utf-8")
    code, _out, err = run(capsys, "comfy", "inspect", run_id, "--write", str(described))
    assert code == 1 and "не перезаписывает" in err


def test_editor_opens_with_a_one_time_address(api, capsys, monkeypatch):
    """`looma comfy editor` берёт билет и открывает редактор; --no-wait — только адрес."""
    base, _state = api
    login(base, capsys)
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    code, out, _err = run(capsys, "comfy", "editor")
    assert code == 0 and opened == ["https://comfyui.apps.example.ru/login?ticket=t1"]
    assert "открыт в браузере" in out
    code, out, _err = run(capsys, "comfy", "editor", "--no-wait")
    assert code == 0 and len(opened) == 1 and "login?ticket=t1" in out and "один раз" in out


def test_submitted_node_set_goes_into_the_graph_description(api, capsys, tmp_path):
    """Набор узлов ComfyUI подключают строкой extensions в описании графа, а не
    флагом --engine, как движок."""
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    folder = tmp_path / "kjnodes"
    (folder / "comfyui_kjnodes").mkdir(parents=True)
    (folder / "comfyui_kjnodes" / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (folder / "looma-extension.yaml").write_text(
        "name: kjnodes\ntitle: KJNodes\nextends: comfyui\npackage: comfyui_kjnodes\n"
        "comfyui:\n  nodes: [A]\n", encoding="utf-8")
    code, out, err = run(capsys, "engine", "submit", str(folder), "--extension")
    assert code == 0, err
    assert "extensions: [anna/kjnodes]" in out and "--engine" not in out
    code, out, err = run(capsys, "engine", "try", str(folder))
    assert code == 0, err
    assert "без ревью" in out and "extensions: [anna/kjnodes]" in out


def test_node_set_folder_may_be_bigger_than_an_engine(tmp_path, monkeypatch):
    from loomafloat import pack as packing

    folder = tmp_path / "kjnodes"
    (folder / "comfyui_kjnodes").mkdir(parents=True)
    (folder / "comfyui_kjnodes" / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (folder / "comfyui_kjnodes" / "font.ttf").write_bytes(__import__("os").urandom(4096))
    (folder / "looma-extension.yaml").write_text(
        "name: kjnodes\ntitle: KJNodes\nextends: comfyui  # набор узлов\npackage: comfyui_kjnodes\n"
        "comfyui:\n  nodes: [A]\n", encoding="utf-8")
    monkeypatch.setattr(packing, "ENGINE_MAX_BYTES", 1024)
    data, kind, files = packing.pack_engine(folder)
    assert kind == "extension" and len(data) > 1024, "у набора узлов предел 64 МБ"
    (folder / "looma-extension.yaml").write_text(
        "name: upper\ntitle: Заглавные\nextends: anna/echo\npackage: comfyui_kjnodes\n",
        encoding="utf-8")
    with pytest.raises(PackError, match="больше 0 МБ"):
        packing.pack_engine(folder)


def test_call_video_waits_and_saves(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    out = tmp_path / "clip.mp4"
    code, text, err = run(capsys, "call", "anna/gifs", "--video", "кот бежит", "--out", str(out))
    assert code == 0, err
    assert f"видео: {out}" in text and out.read_bytes()
    assert state.videos[-1][0]["model"] == "anna/gifs" and state.videos[-1][0]["prompt"] == "кот бежит"


def test_weights_records_and_a_model_from_one(api, capsys):
    """`looma weights`: запись весов закрепляет коммит, модель собирается из неё."""
    base, state = api
    login(base, capsys)
    code, _out, err = run(capsys, "weights", "add", "anna/qwen-legal")
    assert code == 1 and "имя автора" in err
    state.handle = "anna"
    code, out, err = run(capsys, "weights", "add", "https://huggingface.co/anna/qwen-legal",
                         "--summary", "Юридические тексты")
    assert code == 0, err
    assert "anna/qwen-legal: anna/qwen-legal, 3.0 ГиБ, коммит cccccccc, версия 1" in out
    assert "--weights anna/qwen-legal" in out
    code, out, _err = run(capsys, "weights", "add", "anna/qwen-legal")
    assert "коммит тот же, версия 1" in out
    code, out, _err = run(capsys, "weights", "add", "anna/qwen-legal", "--revision", "e" * 40)
    assert "версия 2" in out and "коммит eeeeeeee" in out
    code, _out, err = run(capsys, "weights", "add", "anna/style/lora.ckpt")
    assert code == 1 and "safetensors" in err
    code, out, _err = run(capsys, "weights", "add", "anna/style/lora.safetensors",
                          "--folder", "loras", "--name", "style")
    assert code == 0 and "anna/style/lora.safetensors (папка loras)" in out
    assert "--weights" not in out, "отдельный файл модели на движке не годится"
    code, out, _err = run(capsys, "weights")
    assert "свои веса: 2 из 100" in out and "anna/qwen-legal" in out and "видите только вы" in out
    assert "открытые веса авторов: 1" in out and "bob/whisper" in out
    code, out, _err = run(capsys, "weights", "open", "qwen-legal")
    assert code == 0 and "anna/qwen-legal: открыта" in out
    code, out, _err = run(capsys, "weights", "show", "qwen-legal")
    assert "открыта авторам" in out and "версия 2: коммит eeeeeeeeeeee" in out
    code, out, err = run(capsys, "model", "add", "legal", "--weights", "qwen-legal")
    assert code == 0, err
    sent = [c for c in state.calls if c[0] == "POST" and c[1] == "/api/services"]
    assert json.loads(sent[-1][3])["weights"] == "anna/qwen-legal"
    code, _out, err = run(capsys, "model", "add", "x", "--weights", "anna/qwen-legal",
                          "--repo", "anna/qwen-legal")
    assert code == 1 and "--repo" in err
    code, out, _err = run(capsys, "weights", "rm", "anna/qwen-legal")
    assert code == 0 and "работают дальше" in out


def test_recipes_versions_copy_and_a_model(api, capsys, tmp_path):
    """`looma recipe`: свой рецепт с версиями, копия открытого, модель из рецепта."""
    base, state = api
    login(base, capsys)
    state.handle = "anna"
    preset = tmp_path / "loud.json"
    preset.write_text(json.dumps({"system": "Отвечай громко."}), encoding="utf-8")
    code, _out, err = run(capsys, "recipe", "add", "loud", "--preset", str(preset))
    assert code == 1 and "--base" in err
    code, out, err = run(capsys, "recipe", "add", "loud", "--base", "chat-model",
                         "--preset", str(preset))
    assert code == 0, err
    assert "anna/loud: версия 1, черновик" in out
    preset.write_text(json.dumps({"system": "Ещё громче."}), encoding="utf-8")
    code, out, _err = run(capsys, "recipe", "add", "loud", "--base", "chat-model",
                          "--preset", str(preset))
    assert "версия 2" in out
    code, out, _err = run(capsys, "recipe")
    assert "anna/loud  настройка поверх модели на chat-model  версия 2" in out
    assert "открытые рецепты авторов: 1" in out and "bob/tone" in out
    code, out, _err = run(capsys, "recipe", "copy", "bob/tone", "--name", "tone")
    assert code == 0 and "anna/tone: копия bob/tone" in out
    code, out, _err = run(capsys, "recipe", "show", "tone")
    assert "основано на bob/tone, версия 1" in out
    code, out, _err = run(capsys, "recipe", "publish", "loud")
    assert "отправлен на проверку" in out
    code, _out, err = run(capsys, "recipe", "model", "loud")
    assert code == 1 and "--name" in err
    code, out, err = run(capsys, "recipe", "model", "tone", "--name", "tone-bot")
    assert code == 0, err
    sent = [c for c in state.calls if c[0] == "POST" and c[1] == "/api/services"]
    assert json.loads(sent[-1][3]) == {"name": "tone-bot", "recipe": "anna/tone"}
    code, out, err = run(capsys, "model", "add", "loud-bot", "--recipe", "loud")
    assert code == 0, err
    sent = [c for c in state.calls if c[0] == "POST" and c[1] == "/api/services"]
    assert json.loads(sent[-1][3])["recipe"] == "anna/loud"
    code, out, _err = run(capsys, "recipe", "rm", "loud")
    assert code == 0 and "anna/loud убран" in out


def test_run_takes_the_recipe_in_the_request(api, capsys, tmp_path, monkeypatch):
    """`looma run`: рецепт полями прямо в запросе, ответ движка в файл, подсказка модели."""
    base, state = api
    login(base, capsys)
    monkeypatch.setattr("loomafloat.cli.time.sleep", lambda s: None)
    code, _out, err = run(capsys, "run", "bob/other", "--set", "tone=loud")
    assert code == 1 and "не принимает" in err
    out_file = tmp_path / "answer.json"
    code, out, err = run(capsys, "run", "bob/echo", "--set", "tone=loud", "--set", "repeat=2",
                         "--input", "text=привет", "--out", str(out_file))
    assert code == 0, err
    sent = state.computes["cmp_1"]["body"]
    assert sent == {"engine": "bob/echo", "recipe": {"tone": "loud", "repeat": 2},
                    "inputs": {"text": "привет"}}
    assert "прогон на движке bob/echo, версия 2" in out
    assert json.loads(out_file.read_text(encoding="utf-8")) == {"output": "ПРИВЕТ"}
    assert "looma model add <имя> --engine bob/echo --set tone=loud --set repeat=2" in out
    code, out, _err = run(capsys, "run", "bob/echo", "--recipe", "bob/tone", "--no-wait")
    assert code == 0 and "cmp_2: прогон на bob/echo принят" in out
    assert state.computes["cmp_2"]["body"]["recipe_ref"] == "bob/tone"
    code, _out, err = run(capsys, "run", "bob/echo", "--recipe", "bob/tone", "--set", "a=1")
    assert code == 1 and "что-то одно" in err
    picture = tmp_path / "cat.png"
    picture.write_bytes(b"\x89PNGcat")
    code, out, err = run(capsys, "run", "bob/echo", "--file", f"image={picture}", "--no-wait")
    assert code == 0, err
    assert state.computes["cmp_3"]["body"]["files"] == {"image": "file-1"}
    assert b"\x89PNGcat" in state.uploads[0] and b'name="purpose"' in state.uploads[0]
    code, _out, err = run(capsys, "run", "bob/echo", "--file", "image=нет.png")
    assert code == 1 and "файла нет" in err
    code, out, _err = run(capsys, "run")
    assert "cmp_1: succeeded" in out and "cmp_2" in out


def test_access_lists_and_revokes_service_accesses(api, capsys):
    """Человек смотрит, каким сервисам разрешил действовать в каталоге, и отзывает."""
    base, state = api
    login(base, capsys)
    code, out, err = run(capsys, "access")
    assert code == 0, err
    assert "Куратор рецептов (curator): черновики в каталоге" in out and "revoke" in out
    code, _out, err = run(capsys, "access", "revoke")
    assert code == 1 and "номер" in err
    code, out, err = run(capsys, "access", "revoke", "7")
    assert code == 0 and "со следующего запроса" in out, err
    assert ("DELETE", "/api/market/consents/7") in [c[:2] for c in state.calls]
    code, out, _err = run(capsys, "access")
    assert "не действуют" in out
