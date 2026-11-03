"""`looma dev`: процессы получают то же окружение, что на платформе, а модели
вызывают dev-токеном. Подставной API вместо сервера; база и бакет в Docker —
отдельной проверкой (LOOMA_TEST_DOCKER=1)."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

import fake_api
from loomafloat import Looma, dev
from test_cli import api, login, run  # noqa: F401  (фикстура)

DEVKIT = str(Path(__file__).resolve().parents[1])

JOB = """
import json, os
from loomafloat import Looma
keys = ("LOOMA_APP", "LOOMA_ENVIRONMENT", "LOOMA_PROCESS", "LOOMA_APP_TOKEN", "LOOMA_API_URL",
        "LOOMA_API_KEY", "SECRET_X", "LOOMA_HACK", "AWS_ACCESS_KEY_ID", "DATABASE_URL", "PORT")
print("ENV " + json.dumps({k: os.environ.get(k) for k in keys}), flush=True)
print("CHAT " + Looma().chat("chat-model", "hi"), flush=True)
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _project(tmp_path: Path, name: str = "devapp") -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / "looma.yaml").write_text(f"name: {name}\nruntime: {{ kind: python }}\n")
    (root / "job.py").write_text(JOB)
    (root / ".env").write_text("# секреты для dev\nexport SECRET_X='42'\nLOOMA_HACK=1\n"
                               "AWS_SECRET_ACCESS_KEY=no\n")
    return root


def _job_manifest():
    return {"runtime": {"kind": "python", "python": "3.12"}, "secrets": ["SECRET_X", "MISSING_Y"],
            "processes": {"job": {"kind": "cron", "schedule": "0 3 * * *",
                                  "cmd": f"{shlex.quote(sys.executable)} job.py"}}}


def test_a_process_gets_the_platform_env_and_not_the_authors(api, capsys, tmp_path,
                                                             monkeypatch):
    base, state = api
    login(base, capsys)
    root = _project(tmp_path)
    state.dev_manifest = _job_manifest()
    monkeypatch.setenv("PYTHONPATH", DEVKIT)
    # Чего у процесса на платформе нет: ключ автора, его AWS и база.
    monkeypatch.setenv("LOOMA_API_KEY", fake_api.KEY)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "authors-own")
    monkeypatch.setenv("DATABASE_URL", "postgresql://authors-own")
    code, out, err = run(capsys, "--api", base, "dev", "run", "job", "--no-data",
                         "--dir", str(root))
    assert code == 0, out + err
    assert "заведено приложение devapp" in out and "MISSING_Y" in out
    assert state.dev_texts == [(root / "looma.yaml").read_text()]
    import json
    env = json.loads(next(line for line in out.splitlines() if "ENV " in line).split("ENV ", 1)[1])
    assert env["LOOMA_APP"] == "devapp" and env["LOOMA_ENVIRONMENT"] == "dev"
    assert env["LOOMA_PROCESS"] == "job" and env["LOOMA_APP_TOKEN"] == fake_api.DEV_TOKEN
    assert env["LOOMA_API_URL"] == base + "/v1" and env["SECRET_X"] == "42"
    for hidden in ("LOOMA_API_KEY", "LOOMA_HACK", "AWS_ACCESS_KEY_ID", "DATABASE_URL", "PORT"):
        assert env[hidden] is None, hidden
    # Модель вызвана dev-токеном.
    assert "CHAT эхо: hi" in out and state.dev_calls == 1
    assert "job     │" in out


def test_up_runs_web_and_worker_until_stopped(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    root = _project(tmp_path)
    port = _free_port()
    state.dev_manifest = {
        "runtime": {"kind": "python", "python": "3.12"},
        "processes": {
            "web": {"kind": "web", "port": port,
                    "cmd": f"{shlex.quote(sys.executable)} -m http.server $PORT --bind 127.0.0.1"},
            "worker": {"kind": "worker", "cmd": "echo worker $LOOMA_PROCESS; sleep 30"},
            "nightly": {"kind": "cron", "schedule": "0 3 * * *", "cmd": "echo never"}}}
    lines, stop = [], threading.Event()
    looma = Looma(api_url=base)
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("code", dev.up(
        looma, root, with_data=False, say=lines.append, stop=stop)))
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                assert urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).status == 200
                break
            except OSError:
                assert time.monotonic() < deadline, lines
                time.sleep(0.2)
        while not any("worker  │ worker worker" in line for line in lines):
            assert time.monotonic() < deadline, lines
            time.sleep(0.1)
    finally:
        stop.set()
        thread.join(30)
    assert not thread.is_alive()
    assert any(f"http://127.0.0.1:{port}/" in line for line in lines)
    assert any("looma dev run nightly" in line for line in lines)
    assert not any("never" in line for line in lines)
    with pytest.raises(OSError):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)


def test_a_busy_port_is_named(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    root = _project(tmp_path)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        state.dev_manifest = {"runtime": {"kind": "python"}, "processes": {
            "web": {"kind": "web", "port": port, "cmd": "true"}}}
        code, _out, err = run(capsys, "--api", base, "dev", "--no-data", "--dir", str(root))
    assert code == 1 and f"порт {port} занят" in err


def test_env_prints_only_exports_to_stdout(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    root = _project(tmp_path)
    state.dev_manifest = _job_manifest()
    code, out, err = run(capsys, "--api", base, "dev", "env", "--no-data", "--dir", str(root))
    assert code == 0, err
    assert out.strip() and all(line.startswith("export ") for line in out.splitlines())
    assert f"export LOOMA_APP_TOKEN={fake_api.DEV_TOKEN}" in out
    assert "export SECRET_X=42" in out and "MISSING_Y" in err
    shown = subprocess.run(["/bin/sh", "-c", out + "\necho $LOOMA_ENVIRONMENT"],
                           capture_output=True, text=True)
    assert shown.stdout.strip() == "dev"


def test_the_cli_keeps_its_own_key_after_eval_of_dev_env(api, capsys, monkeypatch):
    base, _state = api
    login(base, capsys)
    # Как после `eval "$(looma dev env)"`: в терминале — токен приложения.
    monkeypatch.setenv("LOOMA_APP_TOKEN", fake_api.DEV_TOKEN)
    monkeypatch.setenv("LOOMA_API_URL", base + "/v1")
    code, out, err = run(capsys, "whoami")
    assert code == 0 and "author@example.ru" in out, err


def test_down_revokes_the_dev_tokens(api, capsys, tmp_path):
    base, _state = api
    login(base, capsys)
    root = _project(tmp_path)
    run(capsys, "--api", base, "dev", "env", "--no-data", "--dir", str(root))
    code, out, err = run(capsys, "--api", base, "dev", "down", "--dir", str(root))
    assert code == 0, err
    assert "dev-токенов отозвано: 2" in out


def test_a_bad_manifest_comes_back_from_the_server(api, capsys, tmp_path):
    base, _state = api
    login(base, capsys)
    code, _out, err = run(capsys, "--api", base, "dev", "--dir", str(tmp_path))
    assert code == 1 and "looma.yaml" in err


def test_docker_mode_passes_values_by_name_and_binds_loopback():
    found = {"app": "bot", "manifest": {"processes": {
        "web": {"kind": "web", "port": 8080, "cmd": "python app.py"},
        "worker": {"kind": "worker", "cmd": ""}}}}
    args = dev.docker_command(found, "web", "looma-dev/bot:latest",
                              {"SECRET": "s3cr3t", "PORT": "8080"})
    assert "s3cr3t" not in " ".join(args)
    assert args[args.index("-p") + 1] == "127.0.0.1:8080:8080"
    assert ["-e", "SECRET"] == args[args.index("SECRET") - 1:args.index("SECRET") + 1]
    assert args[-3:] == ["/bin/sh", "-c", "python app.py"]
    # Без cmd — команда образа.
    assert dev.docker_command(found, "worker", "img", {})[-1] == "img"


def test_data_env_inside_and_outside_docker():
    data = {"postgres": {"container": "looma-dev-bot-postgres", "port": 54321},
            "bucket": {"container": "looma-dev-bot-s3", "port": 54322, "bucket": "bot-dev"}}
    outside = dev.data_env("bot", data, inside=False)
    inside = dev.data_env("bot", data, inside=True)
    assert outside["DATABASE_URL"] == "postgresql://app:app@127.0.0.1:54321/app"
    assert inside["DATABASE_URL"] == "postgresql://app:app@looma-dev-bot-postgres:5432/app"
    assert outside["AWS_ENDPOINT_URL_S3"] == "http://127.0.0.1:54322"
    assert inside["AWS_ENDPOINT_URL_S3"] == "http://looma-dev-bot-s3:8333"
    assert inside["LOOMA_BUCKET"] == "bot-dev"
    local = Looma(api_url="http://127.0.0.1:8000", key="k")
    assert dev.api_for(local, inside=True) == "http://host.docker.internal:8000/v1"
    assert dev.api_for(local, inside=False) == "http://127.0.0.1:8000/v1"


def test_dotenv_is_read_like_a_shell_would(tmp_path):
    (tmp_path / ".env").write_text("A=1\nexport B=\"two words\"\n# C=3\n\nbad line\nD='x=y'\n")
    assert dev.read_dotenv(tmp_path) == {"A": "1", "B": "two words", "D": "x=y"}
    assert dev.read_dotenv(tmp_path / "nope") == {}


@pytest.mark.skipif(os.environ.get("LOOMA_TEST_DOCKER") != "1",
                    reason="настоящий Docker: LOOMA_TEST_DOCKER=1")
def test_real_database_and_bucket_in_docker():
    app = f"t{int(time.time()) % 100000}"
    say = []
    try:
        found = dev.data_up(app, {"data": {"postgres": {"extensions": ["vector"]}, "bucket": {}}},
                            say=say.append)
        again = dev.data_up(app, {"data": {"postgres": {"extensions": ["vector"]}, "bucket": {}}},
                            say=say.append)
        assert found == again                  # повторный запуск — те же порты
        name = found["postgres"]["container"]
        # Роль приложения — не суперпользователь, а vector уже стоит.
        check = subprocess.run(
            ["docker", "exec", "-e", "PGPASSWORD=app", name, "psql", "-h", "127.0.0.1", "-U",
             "app", "-d", "app", "-q", "-tA", "-v", "ON_ERROR_STOP=1",
             "-c", "CREATE TABLE t (v vector(2))", "-c", "INSERT INTO t VALUES ('[1,2]')",
             "-c", "SELECT count(*) FROM t",
             "-c", "SELECT rolsuper FROM pg_roles WHERE rolname = 'app'"],
            capture_output=True, text=True)
        assert check.returncode == 0, check.stderr
        assert check.stdout.split() == ["1", "f"]
        with socket.create_connection(("127.0.0.1", found["postgres"]["port"]), timeout=2):
            pass
        assert dev._s3_up(found["bucket"]["port"])
        listed = subprocess.run(["docker", "exec", "-i", found["bucket"]["container"], "weed",
                                 "shell", "-master=127.0.0.1:9333"], input="s3.bucket.list\n",
                                capture_output=True, text=True)
        assert f"{app}-dev" in listed.stdout
    finally:
        dev.data_down(app, wipe=True)
    assert subprocess.run(["docker", "inspect", f"looma-dev-{app}-postgres"],
                          capture_output=True).returncode != 0


#: Dockerfile платформы для python 3.12 (api/services/app_build.py оркестратора).
PLATFORM_DOCKERFILE = "\n".join([
    "FROM python:3.12-slim", "WORKDIR /app",
    "ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1", "COPY . /app",
    "RUN if [ -f requirements.txt ]; then pip install -r requirements.txt; "
    "elif [ -f pyproject.toml ]; then pip install .; fi", ""])

PROBE = """
import json, os, socket
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            socket.create_connection((os.environ["PGHOST"], int(os.environ["PGPORT"])), 2).close()
            db = "ok"
        except Exception as exc:
            db = repr(exc)
        body = json.dumps({"env": os.environ.get("LOOMA_ENVIRONMENT"), "db": db,
                           "api": os.environ.get("LOOMA_API_URL"),
                           "secret": os.environ.get("SECRET_X"), "cwd": os.getcwd()}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
"""


@pytest.mark.skipif(os.environ.get("LOOMA_TEST_DOCKER") != "1",
                    reason="настоящий Docker: LOOMA_TEST_DOCKER=1")
def test_docker_mode_builds_and_runs_like_the_platform(api, tmp_path):
    import json
    base, state = api
    app = f"d{int(time.time()) % 100000}"
    root = tmp_path / app
    root.mkdir()
    (root / "looma.yaml").write_text(f"name: {app}\n")
    (root / "app.py").write_text(PROBE)
    (root / ".env").write_text("SECRET_X=from-dotenv\n")
    (root / "notes.log").write_text("не для образа")
    (root / ".loomaignore").write_text("*.log\n")
    port = _free_port()
    state.dev_dockerfile = PLATFORM_DOCKERFILE
    state.dev_manifest = {"runtime": {"kind": "python", "python": "3.12"},
                          "data": {"postgres": {"extensions": []}},
                          "processes": {"web": {"kind": "web", "port": port,
                                                "cmd": "python app.py"}}}
    looma, stop, lines = Looma(api_url=base, key=fake_api.KEY), threading.Event(), []
    thread = threading.Thread(target=lambda: dev.up(looma, root, docker_mode=True,
                                                    say=lines.append, stop=stop))
    thread.start()
    try:
        deadline, answer = time.monotonic() + 240, None
        while answer is None:
            assert thread.is_alive() and time.monotonic() < deadline, lines[-20:]
            try:
                answer = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/",
                                                           timeout=2).read())
            except OSError:
                time.sleep(1)
        assert answer["env"] == "dev" and answer["db"] == "ok" and answer["cwd"] == "/app"
        assert answer["secret"] == "from-dotenv"
        assert answer["api"].startswith("http://host.docker.internal:")
        files = subprocess.run(["docker", "run", "--rm", f"looma-dev/{app}:latest", "ls", "-a"],
                               capture_output=True, text=True).stdout.split()
        assert "app.py" in files and "notes.log" not in files and ".env" not in files
    finally:
        stop.set()
        thread.join(60)
        dev.data_down(app, wipe=True)
    assert not thread.is_alive()
    assert subprocess.run(["docker", "image", "inspect", f"looma-dev/{app}:latest"],
                          capture_output=True).returncode != 0
