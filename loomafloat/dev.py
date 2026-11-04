"""`looma dev`: сервис на своей машине так же, как на платформе.

    looma dev                 база и бакет — в Docker, web и worker — здесь
    looma dev --docker        процессы — в образе, собранном как на платформе
    looma dev run nightly     один процесс один раз (cron — вне расписания)
    looma dev env             переменные процесса: для IDE и своего запуска
    looma dev down [--wipe]   остановить базу и бакет (--wipe — удалить с данными)

Процесс получает то же, что на платформе: `PORT`, `DATABASE_URL` и `PG*`,
`AWS_*` и `LOOMA_BUCKET`, `LOOMA_APP_TOKEN` и `LOOMA_API_URL`, `LOOMA_APP`,
`LOOMA_PROCESS`; `LOOMA_ENVIRONMENT` — `dev`. Модели вызываются dev-токеном
сервиса: только объявленные в gpu, с дневной квотой на сервис.
Манифест разбирает сервер — ошибки те же, что у `looma deploy`.

Секреты — из `.env` в папке проекта (в релиз он не уезжает); значения с
платформы сюда не приходят никогда. Ключ автора (`LOOMA_API_KEY`) и его
`AWS_*` процессу не достаются: на платформе их у сервиса тоже нет.

База и бакет — контейнеры `looma-dev-<имя>-postgres` и `looma-dev-<имя>-s3`
из тех же образов, что у платформы; данные — в томах Docker и переживают
перезапуск, порты — только на 127.0.0.1.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

from loomafloat.client import Looma
from loomafloat.pack import MANIFEST, manifest_name, pack

POSTGRES_IMAGE = "pgvector/pgvector:0.8.0-pg16"
S3_IMAGE = "chrislusf/seaweedfs:4.47_large_disk"
#: Только на 127.0.0.1 и только для своей машины — пароли не секрет.
DB_NAME = DB_USER = DB_PASSWORD = "app"
SUPER_PASSWORD = "looma-dev"
S3_KEY, S3_SECRET, REGION = "looma-dev", "looma-dev-secret", "us-east-1"
ENVIRONMENT = "dev"
#: Чего у процесса на платформе нет — с машины автора не передаётся.
HIDDEN_PREFIXES = ("LOOMA_", "AWS_", "S3_", "PG")
HIDDEN = ("DATABASE_URL",)
RESERVED = ("PATH", "HOME", "PORT", "DATABASE_URL", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "NO_PROXY")
_DOTENV = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

Say = Callable[[str], None]


class DevError(RuntimeError):
    """Не запустить — словами."""


# ---------------------------------------------------------------- сессия
def session(looma: Looma, root: Path, *, say: Say) -> dict:
    """Dev-токен и разобранный сервером манифест; сервиса нет — завести."""
    root = Path(root).resolve()
    path = root / MANIFEST
    if not path.is_file():
        raise DevError(f"в {root} нет {MANIFEST}: начните с `looma init`")
    name = manifest_name(root)
    if not name:
        raise DevError(f"в {MANIFEST} нет строки `name: …`")
    if looma.apps.find(name) is None:
        looma.apps.create(name)
        say(f"заведён сервис {name} (релизов пока нет — `looma deploy`)")
    found = looma.apps.dev(name, path.read_text(encoding="utf-8"))
    found["root"] = str(root)
    return found


def read_dotenv(root: Path) -> Dict[str, str]:
    """`.env` проекта: ИМЯ=значение по строке, `#` — комментарий."""
    values: Dict[str, str] = {}
    try:
        lines = (Path(root) / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        found = _DOTENV.match(line)
        if not found:
            continue
        value = found.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[found.group(1)] = value
    return values


def secrets_of(root: Path, wanted: List[str], *, say: Say) -> Dict[str, str]:
    values = {k: v for k, v in read_dotenv(root).items()
              if k not in RESERVED and not k.startswith(("LOOMA_", "AWS_", "S3_"))}
    missing = [n for n in wanted if n not in values]
    if missing:
        say(f"секретов нет в .env: {', '.join(missing)} — процессы их не увидят")
    return values


# ---------------------------------------------------------------- Docker
def docker(*args: str, check: bool = True, input: Optional[bytes] = None,
           timeout: float = 600.0) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(["docker", *args], input=input, capture_output=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise DevError("нет команды docker: базе и бакету нужен Docker "
                       "(или `looma dev --no-data`)") from None
    if check and done.returncode != 0:
        error = done.stderr.decode(errors="replace").strip().splitlines()
        raise DevError(f"docker {args[0]}: {error[-1] if error else done.returncode}")
    return done


def docker_ready() -> None:
    if shutil.which("docker") is None:
        raise DevError("нет команды docker: базе и бакету нужен Docker "
                       "(или `looma dev --no-data`)")
    if docker("info", "--format", "{{.ServerVersion}}", check=False, timeout=30).returncode:
        raise DevError("Docker не отвечает: запустите Docker Desktop (или `looma dev --no-data`)")


def state_of(container: str) -> Optional[str]:
    done = docker("inspect", "-f", "{{.State.Status}}", container, check=False, timeout=30)
    return done.stdout.decode().strip() if done.returncode == 0 else None


def host_port(container: str, inner: int) -> int:
    text = docker("port", container, f"{inner}/tcp", timeout=30).stdout.decode()
    return int(text.strip().splitlines()[0].rsplit(":", 1)[1])


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def port_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def names(app: str) -> dict:
    base = f"looma-dev-{app}"
    return {"network": base, "postgres": f"{base}-postgres", "s3": f"{base}-s3",
            "image": f"looma-dev/{app}:latest", "bucket": f"{app}-dev"}


def _network(app: str) -> str:
    net = names(app)["network"]
    if docker("network", "inspect", net, check=False, timeout=30).returncode:
        docker("network", "create", "--label", f"looma.dev={app}", net, timeout=60)
    return net


def _run_container(app: str, name: str, inner: int, volume: str, image: str, *args: str,
                   say: Say) -> None:
    """Контейнер данных: есть — запустить, нет — создать с томом `name`."""
    state = state_of(name)
    if state is None:
        say(f"{name}: создаю (в первый раз Docker скачает образ {image})")
        docker("run", "-d", "--name", name, "--label", f"looma.dev={app}",
               "--network", _network(app), "-p", f"127.0.0.1:{free_port()}:{inner}",
               "-v", f"{name}:{volume}", *args, timeout=1800)
    elif state != "running":
        docker("start", name, timeout=120)


def ensure_postgres(app: str, extensions: List[str], *, say: Say) -> dict:
    name = names(app)["postgres"]
    _run_container(app, name, 5432, "/var/lib/postgresql/data", POSTGRES_IMAGE,
                   "-e", f"POSTGRES_PASSWORD={SUPER_PASSWORD}", POSTGRES_IMAGE, say=say)
    deadline = time.monotonic() + 90
    # По TCP: на первом старте образ поднимает временный сервер только на сокете.
    while docker("exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres",
                 check=False, timeout=30).returncode:
        if time.monotonic() > deadline:
            raise DevError(f"{name}: база не поднялась за 90 с — `docker logs {name}`")
        time.sleep(1)

    def psql(sql: str, database: str = "postgres") -> str:
        return docker("exec", name, "psql", "-U", "postgres", "-h", "127.0.0.1", "-d", database,
                      "-v", "ON_ERROR_STOP=1", "-tAc", sql, timeout=60).stdout.decode().strip()

    # Как на платформе: база и роль сервиса, расширения ставит администратор.
    psql(f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{DB_USER}') THEN "
         f"CREATE ROLE {DB_USER} LOGIN PASSWORD '{DB_PASSWORD}'; END IF; END $$")
    if psql(f"SELECT 1 FROM pg_database WHERE datname = '{DB_NAME}'") != "1":
        psql(f"CREATE DATABASE {DB_NAME} OWNER {DB_USER}")
    for extension in extensions:
        psql(f'CREATE EXTENSION IF NOT EXISTS "{extension}"', DB_NAME)
    return {"container": name, "port": host_port(name, 5432)}


def ensure_bucket(app: str, *, say: Say) -> dict:
    name, bucket = names(app)["s3"], names(app)["bucket"]
    config = ('{"identities":[{"name":"dev","credentials":[{"accessKey":"%s","secretKey":"%s"}],'
              '"actions":["Admin","Read","List","Tagging","Write"]}]}' % (S3_KEY, S3_SECRET))
    _run_container(app, name, 8333, "/data", S3_IMAGE, "--entrypoint", "/bin/sh", S3_IMAGE, "-c",
                   f"printf '%s' '{config}' > /tmp/s3.json && exec weed server -dir=/data "
                   "-ip=127.0.0.1 -ip.bind=0.0.0.0 -master.volumeSizeLimitMB=256 -volume.max=0 "
                   "-filer -s3 -s3.config=/tmp/s3.json", say=say)
    port = host_port(name, 8333)
    deadline = time.monotonic() + 90
    while True:
        listed = docker("exec", "-i", name, "weed", "shell", "-master=127.0.0.1:9333",
                        input=b"s3.bucket.list\n", check=False, timeout=60)
        if listed.returncode == 0 and _s3_up(port):
            if bucket in listed.stdout.decode(errors="replace").split():
                break
            made = docker("exec", "-i", name, "weed", "shell", "-master=127.0.0.1:9333",
                          input=f"s3.bucket.create -name {bucket}\n".encode(), check=False,
                          timeout=60)
            if b"created bucket" in made.stdout:
                break
        if time.monotonic() > deadline:
            raise DevError(f"{name}: хранилище не поднялось за 90 с — `docker logs {name}`")
        time.sleep(1)
    return {"container": name, "port": port, "bucket": bucket}


def _s3_up(port: int) -> bool:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
    except urllib.error.HTTPError:
        return True                      # 403 без подписи — значит, отвечает
    except OSError:
        return False
    return True


def data_up(app: str, manifest: dict, *, say: Say) -> dict:
    data = manifest.get("data") or {}
    if not data:
        return {}
    docker_ready()
    found = {}
    if "postgres" in data:
        found["postgres"] = ensure_postgres(app, (data["postgres"] or {}).get("extensions") or [],
                                            say=say)
    if "bucket" in data:
        found["bucket"] = ensure_bucket(app, say=say)
    return found


def data_down(app: str, *, wipe: bool = False) -> List[str]:
    done = []
    every = names(app)
    for kind in ("postgres", "s3"):
        name = every[kind]
        if state_of(name) is None:
            continue
        if wipe:
            docker("rm", "-f", name, timeout=120)
            docker("volume", "rm", "-f", name, check=False, timeout=60)
        else:
            docker("stop", name, timeout=120)
        done.append(name)
    if wipe:
        docker("network", "rm", every["network"], check=False, timeout=60)
        # `rmi -f` молчит и о несуществующем образе — сначала спросить, есть ли он.
        if docker("image", "inspect", every["image"], check=False, timeout=60).returncode == 0:
            docker("rmi", "-f", every["image"], check=False, timeout=120)
            done.append(every["image"])
    return done


# ------------------------------------------------------------- окружение
def data_env(app: str, data: dict, *, inside: bool) -> Dict[str, str]:
    """Переменные данных. `inside` — для процесса в контейнере той же сети."""
    env: Dict[str, str] = {}
    if "postgres" in data:
        host = data["postgres"]["container"] if inside else "127.0.0.1"
        port = 5432 if inside else data["postgres"]["port"]
        env.update(DATABASE_URL=f"postgresql://{DB_USER}:{DB_PASSWORD}@{host}:{port}/{DB_NAME}",
                   PGHOST=host, PGPORT=str(port), PGDATABASE=DB_NAME, PGUSER=DB_USER,
                   PGPASSWORD=DB_PASSWORD)
    if "bucket" in data:
        endpoint = (f"http://{data['bucket']['container']}:8333" if inside
                    else f"http://127.0.0.1:{data['bucket']['port']}")
        env.update(AWS_ACCESS_KEY_ID=S3_KEY, AWS_SECRET_ACCESS_KEY=S3_SECRET,
                   AWS_ENDPOINT_URL_S3=endpoint, AWS_REGION=REGION, AWS_DEFAULT_REGION=REGION,
                   LOOMA_BUCKET=data["bucket"]["bucket"])
    return env


def api_for(looma: Looma, *, inside: bool) -> str:
    url = looma.base + "/v1"
    if inside:
        parts = urllib.parse.urlsplit(url)
        if parts.hostname in ("localhost", "127.0.0.1", "::1"):
            netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
            url = urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    return url


def process_env(dev: dict, process: str, *, api_url: str, data: Dict[str, str],
                secrets: Dict[str, str], web_port: int) -> Dict[str, str]:
    """Что процесс получает от платформы — здесь те же имена."""
    proc = dev["manifest"]["processes"][process]
    env = dict(secrets)
    env.update(data)
    env.update(LOOMA_APP=dev["app"], LOOMA_APP_ID=dev["app_id"], LOOMA_ENVIRONMENT=ENVIRONMENT,
               LOOMA_RELEASE="0", LOOMA_PROCESS=process, LOOMA_INSTANCE="0",
               LOOMA_APP_TOKEN=dev["token"], LOOMA_API_URL=api_url)
    if web_port:
        env["LOOMA_APP_URL"] = f"http://127.0.0.1:{web_port}/"
    if proc.get("port"):
        env["PORT"] = str(proc["port"])
    return env


def host_base() -> Dict[str, str]:
    """Окружение машины без того, чего у процесса на платформе нет."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(HIDDEN_PREFIXES) and k not in HIDDEN}


# ---------------------------------------------------------------- образ
def build_image(dev: dict, *, say: Say) -> str:
    """Образ как на платформе: те же файлы, что уехали бы в релиз, и тот же
    Dockerfile. Контекст — во временной папке: лишнее из проекта в сборку не идёт."""
    root = Path(dev["root"])
    archive, files, _skipped = pack(root)
    dockerfile = dev["manifest"]["runtime"].get("dockerfile") or "Dockerfile.looma"
    image = names(dev["app"])["image"]
    say(f"сборка образа {image}: {len(files)} файлов")
    with tempfile.TemporaryDirectory(prefix="looma-dev-") as context:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
            for member in source.getmembers():
                if not member.isfile():
                    continue
                target = Path(context, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as handle:
                    target.write_bytes(handle.read())
                target.chmod(member.mode & 0o755 | 0o644)
        if dev.get("dockerfile"):
            Path(context, dockerfile).write_text(dev["dockerfile"])
        try:
            build = subprocess.Popen(["docker", "build", "-t", image, "-f",
                                      str(Path(context, dockerfile)), context],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        except FileNotFoundError:
            raise DevError("нет команды docker: `--docker` собирает образ в Docker") from None
        assert build.stdout is not None
        for line in build.stdout:
            say("build   │ " + line.decode(errors="replace").rstrip())
        if build.wait():
            raise DevError("образ не собрался — причина выше; на платформе было бы то же")
    return image


def docker_command(dev: dict, process: str, image: str, env: Dict[str, str]) -> List[str]:
    """`docker run` процесса: переменные — по имени (значения — из окружения
    CLI, в списке процессов их не видно), порт — только на 127.0.0.1."""
    proc = dev["manifest"]["processes"][process]
    args = ["docker", "run", "--rm", "--init", "--name", f"looma-dev-{dev['app']}-{process}",
            "--label", f"looma.dev={dev['app']}", "--network", names(dev["app"])["network"],
            "--add-host", "host.docker.internal:host-gateway"]
    for key in sorted(env):
        args += ["-e", key]
    if proc.get("port"):
        args += ["-p", f"127.0.0.1:{proc['port']}:{proc['port']}"]
    args.append(image)
    if proc.get("cmd"):
        args += ["/bin/sh", "-c", proc["cmd"]]
    return args


# --------------------------------------------------------------- процессы
class Runner:
    """Процессы с выводом в одну ленту: `web     │ строка`."""

    def __init__(self, say: Say) -> None:
        self.say = say
        self.lock = threading.Lock()
        self.procs: Dict[str, subprocess.Popen] = {}
        self.readers: List[threading.Thread] = []
        self.containers: List[str] = []
        self.stopped = False

    def start(self, label: str, args: List[str], *, env: Dict[str, str], cwd: str) -> None:
        proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=hasattr(os, "killpg"))
        self.procs[label] = proc
        reader = threading.Thread(target=self._pump, args=(label, proc), daemon=True)
        reader.start()
        self.readers.append(reader)

    def _pump(self, label: str, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            with self.lock:
                self.say(f"{label:<8}│ {raw.decode(errors='replace').rstrip()}")
        code = proc.wait()
        with self.lock:
            self.say(f"{label:<8}│ — завершился, код {code}")

    def wait(self, stop: Optional[threading.Event] = None) -> int:
        """Ждать, пока все не завершатся (или `stop`); код — худший из кодов."""
        while any(p.poll() is None for p in self.procs.values()):
            if stop is not None and stop.wait(0.2):
                break
            if stop is None:
                time.sleep(0.2)
        self.stop()
        return max((abs(p.returncode or 0) for p in self.procs.values()), default=0)

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for name in self.containers:
            subprocess.run(["docker", "stop", "-t", "5", name], capture_output=True)
        for proc in self.procs.values():
            if proc.poll() is None:
                _signal(proc, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for proc in self.procs.values():
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                _signal(proc, signal.SIGKILL)
                proc.wait()
        for reader in self.readers:
            reader.join(2)


def _signal(proc: subprocess.Popen, sig: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, sig)
        else:                                  # pragma: no cover — Windows
            proc.terminate()
    except (ProcessLookupError, PermissionError):
        pass


def _command(proc: dict, process: str) -> List[str]:
    cmd = (proc.get("cmd") or "").strip()
    if not cmd:
        raise DevError(f"у процесса {process} нет cmd: без своей машины его запускает только "
                       "образ — `looma dev --docker`")
    return ["/bin/sh", "-c", cmd]


def prepare(looma: Looma, root: Path, *, docker_mode: bool, with_data: bool,
            say: Say) -> dict:
    """Всё до запуска процессов: токен, данные, секреты, образ."""
    dev = session(looma, root, say=say)
    manifest = dev["manifest"]
    if manifest["runtime"]["kind"] == "docker" and not docker_mode:
        say("runtime: docker — процессы запускаются в образе (как с --docker)")
        docker_mode = True
    data = data_up(dev["app"], manifest, say=say) if with_data else {}
    if not with_data and manifest.get("data"):
        say("--no-data: базы и бакета нет, переменных DATABASE_URL и AWS_* тоже")
    dev.update(docker=docker_mode, data=data,
               secrets=secrets_of(Path(dev["root"]), manifest.get("secrets") or [], say=say))
    if docker_mode:
        docker_ready()
        _network(dev["app"])
        dev["image"] = build_image(dev, say=say)
    return dev


def env_for(looma: Looma, dev: dict, process: str) -> Dict[str, str]:
    inside = dev["docker"]
    web = [p for p in dev["manifest"]["processes"].values() if p.get("kind") == "web"]
    return process_env(dev, process, api_url=api_for(looma, inside=inside),
                       data=data_env(dev["app"], dev["data"], inside=inside),
                       secrets=dev["secrets"], web_port=web[0]["port"] if web else 0)


def launch(looma: Looma, dev: dict, runner: Runner, process: str) -> None:
    proc = dev["manifest"]["processes"].get(process)
    if proc is None:
        raise DevError(f"процесса {process} нет; есть: {', '.join(dev['manifest']['processes'])}")
    env = env_for(looma, dev, process)
    if proc.get("port") and port_busy(proc["port"]):
        raise DevError(f"порт {proc['port']} занят: освободите его или поменяйте port "
                       f"процесса {process} в {MANIFEST}")
    if dev["docker"]:
        args = docker_command(dev, process, dev["image"], env)
        runner.containers.append(f"looma-dev-{dev['app']}-{process}")
        subprocess.run(["docker", "rm", "-f", runner.containers[-1]], capture_output=True)
        runner.start(process, args, env={**os.environ, **env}, cwd=dev["root"])
    else:
        runner.start(process, _command(proc, process), env={**host_base(), **env},
                     cwd=dev["root"])


def summary(dev: dict) -> List[str]:
    quota = dev.get("quota") or {}
    lines = [f"looma dev · {dev['app']} · модели: {', '.join(dev['models']) or 'нет'}"
             + (f" · сегодня {quota.get('used_today', 0)} из {quota['daily_calls']} вызовов"
                if quota.get("daily_calls") else "")]
    data = dev.get("data") or {}
    if "postgres" in data:
        lines.append(f"база     postgresql://{DB_USER}:{DB_PASSWORD}@127.0.0.1:"
                     f"{data['postgres']['port']}/{DB_NAME}")
    if "bucket" in data:
        lines.append(f"бакет    {data['bucket']['bucket']} · http://127.0.0.1:"
                     f"{data['bucket']['port']}")
    for name, proc in dev["manifest"]["processes"].items():
        if proc.get("kind") == "web":
            lines.append(f"{name:<8} http://127.0.0.1:{proc['port']}/")
        elif proc.get("kind") == "cron":
            lines.append(f"{name:<8} cron «{proc.get('schedule')}» — не по расписанию: "
                         f"`looma dev run {name}`")
    return lines


def up(looma: Looma, root: Path, *, docker_mode: bool = False, with_data: bool = True,
       only: Optional[List[str]] = None, say: Say = print,
       stop: Optional[threading.Event] = None) -> int:
    dev = prepare(looma, root, docker_mode=docker_mode, with_data=with_data, say=say)
    steady = [n for n, p in dev["manifest"]["processes"].items() if p.get("kind") != "cron"]
    chosen = only or steady
    for line in summary(dev):
        say(line)
    if not chosen:
        say("постоянных процессов нет — `looma dev run <процесс>`")
        return 0
    runner = Runner(say)
    try:
        for process in chosen:
            launch(looma, dev, runner, process)
        say("Ctrl+C — остановить")
        return runner.wait(stop)
    finally:
        runner.stop()


def run_once(looma: Looma, root: Path, process: str, *, docker_mode: bool = False,
             with_data: bool = True, say: Say = print) -> int:
    dev = prepare(looma, root, docker_mode=docker_mode, with_data=with_data, say=say)
    runner = Runner(say)
    try:
        launch(looma, dev, runner, process)
        return runner.wait()
    finally:
        runner.stop()


def env_lines(looma: Looma, root: Path, process: Optional[str] = None, *,
              with_data: bool = True, say: Say = print) -> List[str]:
    dev = prepare(looma, root, docker_mode=False, with_data=with_data, say=say)
    processes = dev["manifest"]["processes"]
    process = process or next((n for n, p in processes.items() if p.get("kind") == "web"),
                              next(iter(processes)))
    if process not in processes:
        raise DevError(f"процесса {process} нет; есть: {', '.join(processes)}")
    env = env_for(looma, dev, process)
    return [f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items())]


def down(looma: Looma, root: Path, *, wipe: bool = False, say: Say = print) -> None:
    name = manifest_name(Path(root).resolve())
    if not name:
        raise DevError(f"в {MANIFEST} нет строки `name: …`")
    if shutil.which("docker"):
        stopped = data_down(name, wipe=wipe)
        say(("удалено с данными: " if wipe else "остановлено: ") + (", ".join(stopped) or "нечего"))
    if looma.apps.find(name) is not None:
        gone = looma.apps.dev_revoke(name).get("revoked", 0)
        say(f"dev-токенов отозвано: {gone}")


__all__ = ["DevError", "Runner", "data_env", "docker_command", "down", "env_lines", "names",
           "process_env", "read_dotenv", "run_once", "session", "summary", "up"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit("запускайте через `looma dev`")
