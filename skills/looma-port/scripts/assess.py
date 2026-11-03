#!/usr/bin/env python3
"""Оценка проекта для Looma: подойдёт ли он площадке и что в нём переделать.

    python3 assess.py [папка] [--json]

Только стандартная библиотека Python 3.10+. Ничего не ставит, никуда не ходит
и код проекта не запускает: читает файлы и ищет приметы — зависимости,
импорты, строки кода, сервисы docker-compose. Каждая находка — с местами
«файл:строка», чтобы агент проверил её глазами: примета — повод посмотреть,
а не приговор.

Уровни находок:
    blocker — площадка этого не умеет, и переделкой кода не обойти
    change  — нужно переделать код или конфигурацию
    risk    — может не заработать, проверить на staging
    note    — к сведению, учесть в манифесте

Вывод — отчёт в Markdown или JSON (`--json`) для агента.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

# ------------------------------------------------------------ пределы площадки
#: Как у платформы (looma.yaml, сборка, шлюз): подробности — references/platform.md.
SOURCE_MAX_BYTES = 100 * 1024**2       # архив исходников, сжатый
UNPACKED_MAX_BYTES = 1024**3           # он же распакованный
FILES_MAX = 20000
POSTGRES_EXTENSIONS = ("vector", "pg_trgm", "uuid-ossp", "citext", "hstore", "pgcrypto",
                       "btree_gin", "btree_gist", "unaccent", "fuzzystrmatch")
RESERVED_ENV = {"PATH", "HOME", "PORT", "DATABASE_URL", "TMPDIR", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "NO_PROXY"}
RESERVED_PREFIXES = ("LOOMA_", "AWS_", "S3_")

# ----------------------------------------------------------------- обход файлов
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next", ".nuxt",
             ".svelte-kit", ".cache", "dist", "build", "target", "coverage", ".idea",
             ".vscode", "site-packages", "loomafloat", ".looma", ".terraform"}
TEXT_SUFFIXES = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
                 ".go", ".rs", ".rb", ".php", ".java", ".kt", ".cs", ".ex", ".exs", ".toml",
                 ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".txt", ".sh", ".sql",
                 ".prisma", ".env", ".example", ".sample", ".html"}
TEXT_NAMES = {"dockerfile", "procfile", "makefile", "gemfile", "pipfile", ".env.example",
              ".env.sample", ".env.template", "requirements.txt"}
WEIGHT_SUFFIXES = {".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf", ".ggml", ".onnx",
                   ".h5", ".pkl", ".npz", ".tflite", ".mlmodel"}
READ_MAX = 512 * 1024
WHERE_MAX = 4
#: Тесты, бенчмарки, примеры и документация в образ работающего сервиса не
#: идут: приметы в них не считаются (зависимости проекта — считаются).
AUX_DIRS = {"tests", "test", "__tests__", "spec", "e2e", "benchmarks", "bench", "examples",
            "example", "docs", "doc", "scripts", "notebooks", "fixtures", "samples"}
_AUX_FILE = re.compile(r"(?:^test_.*\.py|.*_test\.py|conftest\.py|.*\.(?:test|spec)\.[jt]sx?|"
                       r"readme.*|changelog.*|license.*)$", re.I)


@dataclass
class Finding:
    id: str
    level: str
    title: str
    detail: str
    fix: str = ""
    where: List[str] = field(default_factory=list)


@dataclass
class Project:
    root: Path
    files: List[Path] = field(default_factory=list)
    size: int = 0
    count: int = 0
    weights: List[Tuple[str, int]] = field(default_factory=list)
    top: Set[str] = field(default_factory=set)                   # файлы в корне проекта
    texts: Dict[str, str] = field(default_factory=dict)          # путь → текст
    py_deps: Dict[str, str] = field(default_factory=dict)        # имя → где объявлено
    node_deps: Dict[str, str] = field(default_factory=dict)
    node_dev_deps: Dict[str, str] = field(default_factory=dict)
    py_imports: Dict[str, List[str]] = field(default_factory=dict)
    node_imports: Dict[str, List[str]] = field(default_factory=dict)
    package: dict = field(default_factory=dict)                  # package.json
    compose: Dict[str, dict] = field(default_factory=dict)       # сервис → {image, build, ...}
    python_version: str = ""
    node_version: str = ""


def rel(project: Project, path: Path) -> str:
    return path.relative_to(project.root).as_posix()


def auxiliary(path: str) -> bool:
    parts = path.lower().split("/")
    return bool(AUX_DIRS & set(parts[:-1])) or bool(_AUX_FILE.match(parts[-1]))


def is_text(path: Path) -> bool:
    name = path.name.lower()
    if name in TEXT_NAMES or name.startswith("dockerfile") or name.startswith("docker-compose") \
            or name.startswith("compose.") or name.startswith("requirements"):
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def walk(root: Path) -> Project:
    project = Project(root=root)
    for top, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(names):
            path = Path(top) / name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            project.count += 1
            project.size += size
            if Path(top) == root:
                project.top.add(name)
            if path.suffix.lower() in WEIGHT_SUFFIXES and size > 20 * 1024**2:
                project.weights.append((rel(project, path), size))
            if is_text(path) and size <= READ_MAX:
                try:
                    project.texts[rel(project, path)] = path.read_text(encoding="utf-8",
                                                                        errors="replace")
                except OSError:
                    pass
                project.files.append(path)
    return project


# -------------------------------------------------------------- зависимости
def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def read_python_deps(project: Project) -> None:
    for path, text in project.texts.items():
        name = path.rsplit("/", 1)[-1].lower()
        if name.startswith("requirements") and name.endswith(".txt"):
            if re.search(r"dev|test|doc|lint|ci", name) or auxiliary(path):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                line = line.split("#", 1)[0].strip()
                if not line or line.startswith("-"):
                    continue
                found = _REQ.match(line.split(" @ ", 1)[0])
                if found:
                    project.py_deps.setdefault(norm(found.group(1)), f"{path}:{number}")
        elif name == "pyproject.toml":
            _pyproject(project, path, text)
        elif name == "pipfile":
            section = ""
            for number, line in enumerate(text.splitlines(), 1):
                header = re.match(r"^\s*\[([^\]]+)\]", line)
                if header:
                    section = header.group(1).strip()
                    continue
                pip = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", line)
                if pip and section in ("packages", "dev-packages"):
                    project.py_deps.setdefault(norm(pip.group(1)), f"{path}:{number}")
        elif name in ("setup.py", "setup.cfg"):
            block = re.search(r"install_requires\s*[=:]\s*\[?(.*?)(?:\]|\n\S|\Z)", text, re.S)
            if block:
                start = text.count("\n", 0, block.start()) + 1
                for item in re.findall(r"""["']?([A-Za-z0-9][A-Za-z0-9._-]*)""", block.group(1)):
                    project.py_deps.setdefault(norm(item), f"{path}:{start}")
        elif name in (".python-version", "runtime.txt"):
            version = re.search(r"(\d+\.\d+)", text)
            if version and not project.python_version:
                project.python_version = version.group(1)


def _pyproject(project: Project, path: str, text: str) -> None:
    lines = text.splitlines()

    def line_of(needle: str) -> str:
        for number, line in enumerate(lines, 1):
            if needle in line:
                return f"{path}:{number}"
        return path

    names: List[str] = []
    try:
        import tomllib  # 3.11+
        data = tomllib.loads(text)
        project_table = data.get("project") or {}
        for item in project_table.get("dependencies") or []:
            names.append(str(item))
        for group in (project_table.get("optional-dependencies") or {}).values():
            names.extend(str(i) for i in group)
        poetry = ((data.get("tool") or {}).get("poetry") or {})
        names.extend(k for k in (poetry.get("dependencies") or {}) if k.lower() != "python")
        requires = str(project_table.get("requires-python") or "")
        if not requires:
            requires = str((poetry.get("dependencies") or {}).get("python") or "")
        version = re.search(r"(\d+\.\d+)", requires)
        if version and not project.python_version:
            project.python_version = version.group(1)
    except Exception:
        block = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.S)
        if block:
            names.extend(re.findall(r"""["']([^"']+)["']""", block.group(1)))
    for item in names:
        found = _REQ.match(item)
        if found:
            project.py_deps.setdefault(norm(found.group(1)), line_of(found.group(1)))


def read_node(project: Project) -> None:
    for path, text in project.texts.items():
        if path.rsplit("/", 1)[-1] != "package.json":
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if path == "package.json" or not project.package:
            project.package = data
        lines = text.splitlines()
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            target = project.node_dev_deps if key == "devDependencies" else project.node_deps
            for dep in (data.get(key) or {}):
                where = path
                for number, line in enumerate(lines, 1):
                    if f'"{dep}"' in line:
                        where = f"{path}:{number}"
                        break
                target.setdefault(dep.lower(), where)
        engines = (data.get("engines") or {}).get("node") or ""
        version = re.search(r"(\d+)", str(engines))
        if version and not project.node_version:
            project.node_version = version.group(1)
    for path, text in project.texts.items():
        if path.rsplit("/", 1)[-1] in (".nvmrc", ".node-version") and not project.node_version:
            version = re.search(r"(\d+)", text)
            if version:
                project.node_version = version.group(1)


_PY_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_][\w]*)|import[ \t]+([A-Za-z_][\w]*))",
                        re.M)
_JS_IMPORT = re.compile(r"""(?:require\(\s*|from\s+|import\s+|import\(\s*)['"]([^'"./][^'"]*)['"]""")


def read_imports(project: Project) -> None:
    for path, text in project.texts.items():
        if auxiliary(path):
            continue
        if path.endswith(".py"):
            for found in _PY_IMPORT.finditer(text):
                name = (found.group(1) or found.group(2)).lower()
                line = text.count("\n", 0, found.start()) + 1
                project.py_imports.setdefault(name, []).append(f"{path}:{line}")
        elif path.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte")):
            for found in _JS_IMPORT.finditer(text):
                spec = found.group(1)
                if spec.startswith("node:"):
                    spec = spec[5:]
                parts = spec.split("/")
                name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
                line = text.count("\n", 0, found.start()) + 1
                project.node_imports.setdefault(name.lower(), []).append(f"{path}:{line}")


# ---------------------------------------------------------------- compose
_SERVICE = re.compile(r"^  ([A-Za-z0-9._-]+):\s*$")


def read_compose(project: Project) -> None:
    """Сервисы docker-compose без YAML-библиотеки: имя, образ, build, privileged.
    Хватает для того, чтобы назвать, что чем заменить."""
    for path, text in project.texts.items():
        name = path.rsplit("/", 1)[-1].lower()
        if not (name.startswith("docker-compose") or name.startswith("compose.")) or \
                not name.endswith((".yml", ".yaml")):
            continue
        in_services, current = False, ""
        for number, line in enumerate(text.splitlines(), 1):
            if re.match(r"^services:\s*$", line):
                in_services = True
                continue
            if in_services and re.match(r"^\S", line):
                in_services = False
            if not in_services:
                continue
            service = _SERVICE.match(line)
            if service:
                current = service.group(1)
                project.compose.setdefault(current, {"where": f"{path}:{number}"})
                continue
            if not current:
                continue
            entry = project.compose[current]
            image = re.match(r"^\s{4}image:\s*['\"]?([^'\"\s]+)", line)
            if image:
                entry["image"] = image.group(1).lower()
            if re.match(r"^\s{4}build:", line):
                entry["build"] = True
            if re.search(r"privileged:\s*true|/var/run/docker\.sock", line):
                entry["privileged"] = f"{path}:{number}"


# ----------------------------------------------------------------- приметы
@dataclass
class Rule:
    id: str
    level: str
    title: str
    detail: str
    fix: str = ""
    py: Tuple[str, ...] = ()           # пакеты PyPI и имена импорта
    node: Tuple[str, ...] = ()         # пакеты npm
    code: Tuple[str, ...] = ()         # регулярные выражения по тексту файлов
    images: Tuple[str, ...] = ()       # образы сервисов docker-compose
    code_in: Tuple[str, ...] = ()      # суффиксы или начала имён файлов для code (пусто — все)
    dev: bool = False                  # считать и devDependencies (инструменты сборки)


RULES: List[Rule] = [
    # --- модели на своей видеокарте
    Rule("local-llm-server", "blocker", "Своя языковая модель на GPU (Ollama, vLLM, llama.cpp)",
         "В песочнице площадки нет GPU. Модели считает сеть Looma: вызовы нужно перевести на "
         "модели каталога по API (OpenAI-совместимый), а свой сервер модели убрать.",
         "references/models.md",
         py=("vllm", "llama-cpp-python", "llama_cpp", "ctransformers", "exllamav2", "ollama",
             "auto-gptq", "awq", "text-generation-inference", "tensorrt-llm", "mlx-lm"),
         node=("ollama", "node-llama-cpp", "@llama-node/core"),
         code=(r"localhost:11434|127\.0\.0\.1:11434|OLLAMA_HOST|OLLAMA_BASE_URL",),
         images=("ollama", "vllm", "text-generation-inference", "llama.cpp", "localai")),
    Rule("cuda", "blocker", "Код рассчитан на CUDA",
         "В песочнице нет GPU: `.cuda()` и `device=\"cuda\"` не заработают. Если это инференс "
         "модели — заменить на модель каталога; если обучение — площадка приложений для него "
         "не подходит.",
         "references/models.md",
         code=(r"\.cuda\(\)|device\s*=\s*['\"]cuda|torch\.device\(\s*['\"]cuda|"
               r"CUDA_VISIBLE_DEVICES|FROM\s+nvidia/|--gpus\b|driver:\s*nvidia",),
         code_in=(".py", "dockerfile", "docker-compose", "compose.")),
    Rule("heavy-ml", "change", "Тяжёлые ML-пакеты (torch, tensorflow, transformers, diffusers)",
         "Сборка идёт с 1 ГБ памяти и до 30 минут, образ вырастет на гигабайты, считать на CPU "
         "медленно. Почти всегда правильнее вызывать модели каталога (текст, картинки, видео, "
         "векторы, ре-ранк) и убрать эти пакеты из зависимостей.",
         "references/models.md",
         py=("torch", "tensorflow", "tensorflow-gpu", "jax", "jaxlib", "transformers", "diffusers",
             "accelerate", "bitsandbytes", "xformers", "onnxruntime-gpu", "keras", "peft",
             "sentence-transformers", "sentence_transformers", "flagembedding", "fastembed",
             "timm", "ultralytics", "paddlepaddle"),
         node=("@tensorflow/tfjs-node", "@tensorflow/tfjs-node-gpu", "@xenova/transformers",
               "@huggingface/transformers", "onnxruntime-node")),
    Rule("speech", "blocker", "Распознавание или синтез речи своей моделью",
         "Модели считаются только в сети Looma. Если в `looma models` нет модели нужного типа — "
         "это блокер; внешний API по HTTPS остаётся вариантом (ключ — секретом).",
         "references/models.md",
         py=("openai-whisper", "whisper", "faster-whisper", "faster_whisper", "whisperx", "tts",
             "coqui-tts", "bark", "nemo-toolkit", "vosk", "silero", "pyannote-audio",
             "pyannote")),
    Rule("external-llm-api", "note", "Внешний API моделей (OpenAI, Anthropic, Google и др.)",
         "Работает и на площадке: HTTPS наружу открыт, ключ — секретом (`looma secrets set`). "
         "Либо перевести на модели Looma: OpenAI-совместимый адрес `$LOOMA_API_URL/v1` и токен "
         "`$LOOMA_APP_TOKEN` — тогда вызовы учитываются на клиента маркетплейса.",
         "references/models.md",
         py=("openai", "anthropic", "google-generativeai", "google-genai", "mistralai", "cohere",
             "groq", "together", "replicate", "langchain-openai", "langchain-anthropic",
             "litellm", "llama-index-llms-openai"),
         node=("openai", "@anthropic-ai/sdk", "@google/generative-ai", "@google/genai",
               "@mistralai/mistralai", "cohere-ai", "groq-sdk", "ai", "@ai-sdk/openai",
               "@langchain/openai", "replicate")),
    # --- протоколы
    Rule("streamlit", "blocker", "Streamlit",
         "Интерфейс Streamlit работает через WebSocket, а шлюз площадки WebSocket пока не "
         "пропускает (501). Вариант — переписать интерфейс на обычный веб (HTTP, SSE).",
         "references/platform.md",
         py=("streamlit",)),
    Rule("websocket", "blocker", "WebSocket",
         "Шлюз площадки WebSocket пока не пропускает (501). Потоковые ответы через SSE и "
         "обычный HTTP работают: если сокет нужен только для стрима или уведомлений — "
         "перевести на SSE или опрос; если на нём держится всё приложение — блокер.",
         "references/platform.md",
         py=("python-socketio", "socketio", "flask-socketio", "flask_socketio", "websockets",
             "channels", "django-channels", "simple-websocket"),
         node=("socket.io", "socket.io-client", "ws", "uwebsockets.js", "@fastify/websocket",
               "express-ws", "@nestjs/websockets", "sockjs", "y-websocket"),
         code=(r"new\s+WebSocket\(|@\w+\.websocket\(|from\s+(?:fastapi|starlette)[\w.]*\s+import"
               r"[^\n]*\bWebSocket\b",)),
    Rule("gradio", "risk", "Gradio",
         "Часть версий Gradio держит соединение через WebSocket, часть — через SSE. Проверить "
         "на staging: если интерфейс не грузится или не отвечает — переписать на обычный веб.",
         "references/platform.md",
         py=("gradio",)),
    Rule("notebook", "blocker", "Jupyter или Voilà",
         "Ноутбук-сервер держится на WebSocket, а шлюз его не пропускает. Логику из ноутбука "
         "нужно вынести в обычное веб-приложение.",
         "references/platform.md",
         py=("jupyter", "jupyterlab", "notebook", "voila", "jupyter-server")),
    Rule("non-http", "blocker", "Сервер не на HTTP (gRPC, TCP, UDP)",
         "Снаружи к приложению доходит только HTTP через шлюз. gRPC и свои TCP/UDP-серверы "
         "площадка не пропускает.",
         "references/platform.md",
         py=("grpcio", "grpcio-tools"),
         node=("@grpc/grpc-js", "grpc", "dgram"),
         code=(r"net\.createServer\(|dgram\.createSocket\(|socket\.socket\([^)]*\)[\s\S]{0,200}?\.bind\(",)),
    Rule("headless-browser", "risk", "Браузер без окна (Playwright, Puppeteer, Selenium)",
         "Песочница gVisor и /dev/shm на 64 МБ: Chromium скорее всего не запустится или будет "
         "падать. Проверить на staging; надёжнее — внешний сервис рендеринга по HTTPS.",
         "references/platform.md",
         py=("playwright", "pyppeteer", "selenium", "undetected-chromedriver"),
         node=("puppeteer", "puppeteer-core", "playwright", "@playwright/test",
               "selenium-webdriver")),
    Rule("smtp", "change", "Отправка почты по SMTP",
         "Наружу открыты только порты 80 и 443: SMTP (25, 465, 587) закрыт. Письма — через "
         "HTTP API почтового сервиса (ключ — секретом).",
         "references/platform.md",
         py=("smtplib", "aiosmtplib", "flask-mail", "django-anymail"),
         node=("nodemailer",),
         code=(r"smtp\.[\w.-]+:\d+|EMAIL_PORT\s*=\s*\d+|SMTP_PORT",)),
    # --- данные
    Rule("sqlite", "change", "SQLite",
         "Диск процесса временный: стирается при перезапуске, сне и новом релизе, а экземпляров "
         "может быть несколько. Базу — в Postgres площадки (`data.postgres`, `DATABASE_URL`).",
         "references/data.md",
         py=("sqlite3", "aiosqlite", "sqlite-utils"),
         node=("sqlite3", "better-sqlite3", "sqlite"),
         code=(r"sqlite:///|sqlite\+\w+://|provider\s*=\s*\"sqlite\"|django\.db\.backends\.sqlite3",)),
    Rule("mysql", "change", "MySQL или MariaDB",
         "На площадке есть только Postgres. Перевести на Postgres: драйвер, строка подключения "
         "из `DATABASE_URL`, проверить SQL на расхождения диалектов.",
         "references/data.md",
         py=("pymysql", "mysqlclient", "mysqldb", "aiomysql", "mysql-connector-python", "asyncmy"),
         node=("mysql", "mysql2"),
         code=(r"mysql(?:\+\w+)?://|provider\s*=\s*\"mysql\"|django\.db\.backends\.mysql",),
         images=("mysql", "mariadb", "percona")),
    Rule("mongo", "change", "MongoDB (большая переделка)",
         "MongoDB на площадке нет, а внешний (Atlas) — на порту 27017, который наружу закрыт. "
         "Переписать хранение на Postgres (JSONB для документов).",
         "references/data.md",
         py=("pymongo", "motor", "mongoengine", "beanie", "odmantic"),
         node=("mongodb", "mongoose", "@typegoose/typegoose"),
         code=(r"mongodb(?:\+srv)?://",),
         images=("mongo", "mongodb")),
    Rule("redis", "change", "Redis (кэш, очереди, pub/sub)",
         "Redis на площадке нет, внешний — на порту 6379, который наружу закрыт. Кэш — в памяти "
         "процесса или в Postgres; очередь задач — таблица в Postgres и процесс `worker`.",
         "references/data.md",
         py=("redis", "aioredis", "rq", "celery", "dramatiq", "huey", "arq", "django-redis",
             "flask-caching", "redis-om"),
         node=("redis", "ioredis", "bull", "bullmq", "bee-queue", "kue", "@upstash/redis"),
         code=(r"redis(?:s)?://",),
         images=("redis", "valkey", "keydb", "dragonfly")),
    Rule("broker", "change", "Брокер сообщений (RabbitMQ, Kafka, NATS)",
         "Брокеров на площадке нет, их порты наружу закрыты. Очередь — таблица в Postgres и "
         "процесс `worker`.",
         "references/data.md",
         py=("pika", "aio-pika", "kombu", "kafka-python", "confluent-kafka", "aiokafka", "nats-py"),
         node=("amqplib", "kafkajs", "nats", "@nestjs/microservices"),
         code=(r"amqps?://|kafka://",),
         images=("rabbitmq", "kafka", "confluentinc", "bitnami/kafka", "nats", "zookeeper")),
    Rule("search-engine", "change", "Поисковый движок (Elasticsearch, OpenSearch, Meilisearch)",
         "Отдельных поисковых движков на площадке нет. Полнотекстовый поиск — в Postgres "
         "(tsvector, `pg_trgm`), смысловой — `vector` и векторы из каталога моделей.",
         "references/data.md",
         py=("elasticsearch", "opensearch-py", "meilisearch", "typesense", "qdrant-client",
             "pymilvus", "weaviate-client", "chromadb", "pinecone-client", "pinecone"),
         node=("@elastic/elasticsearch", "@opensearch-project/opensearch", "meilisearch",
               "typesense", "@qdrant/js-client-rest", "chromadb", "@pinecone-database/pinecone"),
         images=("elasticsearch", "opensearch", "meilisearch", "typesense", "qdrant", "milvus",
                 "weaviate", "chroma")),
    Rule("postgres", "note", "Postgres",
         "Есть на площадке: `data.postgres` в манифесте, строка подключения — `DATABASE_URL`.",
         "references/data.md",
         py=("psycopg2", "psycopg2-binary", "psycopg", "asyncpg", "pg8000"),
         node=("pg", "postgres", "pg-promise", "@neondatabase/serverless"),
         code=(r"postgres(?:ql)?(?:\+\w+)?://|provider\s*=\s*\"postgresql\"|"
               r"django\.db\.backends\.postgresql",),
         images=("postgres", "postgis", "pgvector", "timescale")),
    Rule("s3", "note", "S3-хранилище",
         "Есть на площадке: `data.bucket`, доступы — `AWS_*`, имя — `LOOMA_BUCKET`. Адресация "
         "по пути (path-style) — в настройке клиента S3.",
         "references/data.md",
         py=("boto3", "aiobotocore", "minio", "s3fs"),
         node=("@aws-sdk/client-s3", "aws-sdk", "minio"),
         images=("minio",)),
    Rule("local-files", "change", "Запись файлов на диск",
         "Диск процесса временный: всё записанное пропадёт при перезапуске, сне и релизе, а "
         "у нескольких экземпляров диски разные. Загрузки и результаты — в бакет "
         "(`data.bucket`); временные файлы в /tmp — можно.",
         "references/data.md",
         code=(r"open\([^)\n]*,\s*['\"][wa]b?\+?['\"]|\.save\(\s*os\.path\.join|UPLOAD_FOLDER|"
               r"MEDIA_ROOT|fs\.writeFile(?:Sync)?\(|createWriteStream\(|multer\(\s*\{\s*dest",),
         code_in=(".py", ".js", ".ts", ".mjs", ".cjs")),
    # --- процессы и запуск
    Rule("background", "note", "Фоновые задачи и расписание",
         "На площадке это отдельные процессы манифеста: постоянный `worker` или задача по "
         "расписанию (`schedule`, cron в UTC). Воркеры и cron работают только на prod.",
         "references/manifest.md",
         py=("celery", "rq", "dramatiq", "huey", "arq", "apscheduler", "schedule"),
         node=("bull", "bullmq", "agenda", "node-cron", "cron", "node-schedule", "bree")),
    Rule("frontend-build", "risk", "Сборка фронтенда (Next.js, Nuxt, Angular и др.)",
         "Сборка на площадке — 1 ядро, 1 ГБ памяти, до 30 минут. Крупный фронтенд может не "
         "уложиться; тогда собирать заранее и класть готовую сборку в архив.",
         "references/platform.md",
         node=("next", "nuxt", "@angular/core", "@sveltejs/kit", "gatsby", "@remix-run/node",
               "astro", "vite", "webpack", "@nestjs/core"),
         dev=True),
    Rule("own-auth", "note", "Свои учётные записи пользователей",
         "Работают как есть. Для маркетплейса можно добавить `access: clients`: шлюз пускает "
         "только вошедших на витрине и называет клиента заголовком `X-Looma-Client`.",
         "references/manifest.md",
         py=("flask-login", "django-allauth", "fastapi-users", "authlib", "python-jose", "pyjwt"),
         node=("passport", "next-auth", "@auth/core", "lucia", "jsonwebtoken", "express-session",
               "@clerk/nextjs", "@supabase/supabase-js")),
]


def _dep_hits(names: Iterable[str], deps: Dict[str, str],
              imports: Dict[str, List[str]]) -> List[str]:
    hits: List[str] = []
    for name in names:
        key = norm(name)
        if key in deps:
            hits.append(deps[key])
        for variant in {name.lower(), name.lower().replace("-", "_")}:
            hits.extend(imports.get(variant, [])[:2])
    return hits


def _code_hits(project: Project, patterns: Iterable[str], suffixes: Tuple[str, ...]) -> List[str]:
    hits: List[str] = []
    for pattern in patterns:
        regex = re.compile(pattern, re.I)
        for path, text in project.texts.items():
            base = path.rsplit("/", 1)[-1].lower()
            if suffixes and not (path.endswith(suffixes) or base.startswith(suffixes)):
                continue
            if auxiliary(path):
                continue
            for found in regex.finditer(text):
                hits.append(f"{path}:{text.count(chr(10), 0, found.start()) + 1}")
                if len(hits) >= 20:
                    return hits
    return hits


def _image_hits(project: Project, images: Iterable[str]) -> List[str]:
    hits = []
    for service, entry in project.compose.items():
        image = entry.get("image", "")
        base = image.rsplit("/", 1)[-1].split(":", 1)[0]
        if any(base == i or base.startswith(i) or image.startswith(i) for i in images):
            hits.append(f"{entry['where']} ({service}: {image})")
    return hits


def apply_rules(project: Project) -> List[Finding]:
    found: List[Finding] = []
    for rule in RULES:
        where: List[str] = []
        where += _dep_hits(rule.py, project.py_deps, project.py_imports)
        node_deps = {**project.node_dev_deps, **project.node_deps} if rule.dev else project.node_deps
        where += _dep_hits(rule.node, node_deps, project.node_imports)
        if rule.code:
            where += _code_hits(project, rule.code, rule.code_in)
        if rule.images:
            where += _image_hits(project, rule.images)
        if where:
            unique = list(dict.fromkeys(where))
            found.append(Finding(rule.id, rule.level, rule.title, rule.detail, rule.fix,
                                 unique[:WHERE_MAX] + ([f"…ещё {len(unique) - WHERE_MAX}"]
                                                       if len(unique) > WHERE_MAX else [])))
    return found


# --------------------------------------------------------- особые проверки
_LISTEN_PY = re.compile(r"(?:\.run\(|uvicorn\.run\(|web\.run_app\(|serve\()[^)\n]*"
                        r"(?:host\s*=\s*['\"](?:127\.0\.0\.1|localhost)['\"]|port\s*=\s*\d{2,5})")
_LISTEN_CLI = re.compile(r"(?:--bind|-b)\s+(?:127\.0\.0\.1|localhost)|--host\s+(?:127\.0\.0\.1|localhost)")
_LISTEN_JS = re.compile(r"\.listen\(\s*\d{2,5}\s*(?:,\s*['\"](?:127\.0\.0\.1|localhost)['\"])?")
_PORT_ENV = re.compile(r"process\.env\.PORT|os\.environ(?:\.get)?\(?\[?\s*['\"]PORT|getenv\(\s*['\"]PORT|\$PORT|\$\{PORT")


def check_listen(project: Project) -> Optional[Finding]:
    hits = []
    for path, text in project.texts.items():
        if auxiliary(path):
            continue
        if path.endswith(".py"):
            regexes = (_LISTEN_PY, _LISTEN_CLI)
        elif path.endswith((".js", ".ts", ".mjs", ".cjs")):
            regexes = (_LISTEN_JS,)
        elif path.rsplit("/", 1)[-1].lower() in ("procfile", "dockerfile") or path.endswith(
                (".sh", ".toml", ".json")):
            regexes = (_LISTEN_CLI,)
        else:
            continue
        for regex in regexes:
            for found in regex.finditer(text):
                hits.append(f"{path}:{text.count(chr(10), 0, found.start()) + 1}")
    if not hits:
        return None
    uses_env = any(_PORT_ENV.search(t) for t in project.texts.values())
    detail = ("Веб должен слушать `0.0.0.0` на порту из переменной `PORT` (его же пишут в "
              "`processes.web.port`). Сейчас порт или адрес заданы в коде.")
    if uses_env:
        detail += " Переменная PORT в проекте уже читается — проверить, что везде."
    return Finding("listen", "change", "Адрес и порт веб-сервера", detail,
                   "references/manifest.md", hits[:WHERE_MAX])


_OUT_PORT = re.compile(r"\b(?:https?|wss?|postgres(?:ql)?|mysql|mongodb|redis|amqps?|smtp|ftp|"
                       r"tcp)://(?:[^@\s/'\"]+@)?([A-Za-z0-9.-]+):(\d{2,5})")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


def check_outbound_ports(project: Project) -> Optional[Finding]:
    services = set(project.compose)
    hits = []
    for path, text in project.texts.items():
        if auxiliary(path):
            continue
        for found in _OUT_PORT.finditer(text):
            host, port = found.group(1).lower(), int(found.group(2))
            if port in (80, 443) or host in _LOCAL_HOSTS or host in services or "." not in host \
                    or re.match(r"^(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.", host) \
                    or host.endswith((".local", ".internal", ".example", ".example.com")):
                continue
            hits.append(f"{path}:{text.count(chr(10), 0, found.start()) + 1} ({host}:{port})")
    if not hits:
        return None
    return Finding("outbound-ports", "change", "Обращения наружу на порты, кроме 80 и 443",
                   "Из песочницы наружу открыты только порты 80 и 443 (через прокси). Такие "
                   "адреса не ответят: нужен HTTPS-вариант сервиса или замена.",
                   "references/platform.md", hits[:WHERE_MAX])


def check_compose(project: Project) -> List[Finding]:
    out: List[Finding] = []
    privileged = [e["privileged"] for e in project.compose.values() if e.get("privileged")]
    if privileged:
        out.append(Finding("privileged", "blocker", "Привилегии или Docker внутри контейнера",
                           "Песочница без привилегий: `privileged`, `/var/run/docker.sock`, "
                           "запуск своих контейнеров недоступны.",
                           "references/platform.md", privileged[:WHERE_MAX]))
    built = [f"{e['where']} ({name})" for name, e in project.compose.items() if e.get("build")]
    if len(built) > 1:
        out.append(Finding("many-services", "change", "Несколько сервисов своего кода",
                           "Приложение Looma — один образ и несколько процессов из него "
                           "(web, worker, cron). Сервисы с разным кодом — либо в один образ "
                           "разными командами, либо отдельными приложениями, которые ходят "
                           "друг к другу по HTTPS.",
                           "references/recipes-docker.md", built[:WHERE_MAX]))
    proxies = _image_hits(project, ("nginx", "traefik", "caddy", "haproxy", "certbot"))
    if proxies:
        out.append(Finding("reverse-proxy", "note", "Свой обратный прокси или TLS",
                           "Не нужен: домен, сертификат и маршрутизацию даёт шлюз площадки. "
                           "Если nginx отдаёт статику — её отдаёт само приложение.",
                           "references/recipes-docker.md", proxies[:WHERE_MAX]))
    return out


def check_extensions(project: Project) -> Optional[Finding]:
    hits = []
    for path, text in project.texts.items():
        if auxiliary(path):
            continue
        for found in re.finditer(r"CREATE\s+EXTENSION\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"']?([\w-]+)",
                                 text, re.I):
            name = found.group(1).lower()
            if name not in POSTGRES_EXTENSIONS and name != "if":
                hits.append(f"{path}:{text.count(chr(10), 0, found.start()) + 1} "
                            f"({found.group(1)})")
    if not hits:
        return None
    return Finding("pg-extension", "change", "Расширения Postgres вне списка площадки",
                   "Можно заказать только: " + ", ".join(POSTGRES_EXTENSIONS) + ".",
                   "references/data.md", hits[:WHERE_MAX])


def check_size(project: Project) -> List[Finding]:
    out: List[Finding] = []
    if project.count > FILES_MAX or project.size > UNPACKED_MAX_BYTES:
        out.append(Finding("too-big", "blocker", "Проект больше пределов архива",
                           f"Файлов {project.count}, объём {_mb(project.size)}: площадка "
                           f"принимает до {FILES_MAX} файлов и {_mb(UNPACKED_MAX_BYTES)} "
                           f"распакованными ({_mb(SOURCE_MAX_BYTES)} в архиве). Лишнее — в "
                           "`.loomaignore`.", "references/platform.md"))
    elif project.size > SOURCE_MAX_BYTES:
        out.append(Finding("big", "risk", "Проект близко к пределу архива",
                           f"Без служебных папок {_mb(project.size)}; архив сжатым должен быть "
                           f"до {_mb(SOURCE_MAX_BYTES)}. Лишнее — в `.loomaignore`.",
                           "references/platform.md"))
    if project.weights:
        out.append(Finding("weights", "change", "Веса моделей внутри проекта",
                           "Тяжёлые файлы моделей не везти в архиве: модели — из каталога по "
                           "API, свои данные — в бакет.", "references/models.md",
                           [f"{p} ({_mb(s)})" for p, s in project.weights[:WHERE_MAX]]))
    return out


def _mb(size: int) -> str:
    return f"{size / 1024**2:.0f} МБ" if size < 1024**3 else f"{size / 1024**3:.1f} ГБ"


# ------------------------------------------------------------ стек и запуск
FRAMEWORKS_PY = {"flask": "Flask", "fastapi": "FastAPI", "django": "Django",
                 "starlette": "Starlette", "aiohttp": "aiohttp", "tornado": "Tornado",
                 "sanic": "Sanic", "quart": "Quart", "litestar": "Litestar",
                 "streamlit": "Streamlit", "gradio": "Gradio", "bottle": "Bottle",
                 "falcon": "Falcon", "chainlit": "Chainlit"}
FRAMEWORKS_NODE = {"express": "Express", "fastify": "Fastify", "koa": "Koa", "@hapi/hapi": "hapi",
                   "next": "Next.js", "nuxt": "Nuxt", "@nestjs/core": "NestJS",
                   "@sveltejs/kit": "SvelteKit", "@remix-run/node": "Remix", "hono": "Hono",
                   "astro": "Astro"}
OTHER_LANGS = {"go.mod": "Go", "Cargo.toml": "Rust", "pom.xml": "Java", "build.gradle": "Java",
               "build.gradle.kts": "Kotlin", "Gemfile": "Ruby", "composer.json": "PHP",
               "mix.exs": "Elixir", "Package.swift": "Swift"}


def stack(project: Project) -> dict:
    top = project.top
    dockerfiles = sorted(p for p in project.texts if p.rsplit("/", 1)[-1].lower().startswith(
        "dockerfile"))
    python = bool(project.py_deps) or any(p.endswith(".py") for p in project.texts)
    node = bool(project.package)
    top_python = bool(top & {"requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
                             "manage.py", "app.py", "main.py"})
    top_node = "package.json" in top
    other = sorted({lang for name, lang in OTHER_LANGS.items() if name in top})
    py_frameworks = sorted({FRAMEWORKS_PY[k] for k in FRAMEWORKS_PY
                            if k in project.py_deps or k in project.py_imports})
    node_frameworks = sorted({v for k, v in FRAMEWORKS_NODE.items() if k in project.node_deps})
    if other:
        kind = "docker"
    elif top_python and top_node:
        kind = "node" if node_frameworks and not py_frameworks else "python"
    elif top_python:
        kind = "python"
    elif top_node:
        kind = "node"
    elif node and not (python and py_frameworks and not node_frameworks):
        kind = "node"
    elif python:
        kind = "python"
    elif dockerfiles:
        kind = "docker"
    else:
        kind = "unknown"
    return {"kind": kind, "languages": (["Python"] if python else []) + (["Node.js"] if node
                                                                          else []) + other,
            "frameworks": py_frameworks + node_frameworks, "dockerfiles": dockerfiles,
            "has_manifest": "looma.yaml" in project.texts,
            "python_version": project.python_version, "node_version": project.node_version}


def check_stack(project: Project, info: dict) -> List[Finding]:
    out: List[Finding] = []
    others = [lang for lang in info["languages"] if lang not in ("Python", "Node.js")]
    if others and not info["dockerfiles"]:
        out.append(Finding("needs-dockerfile", "change", f"Язык {', '.join(others)} без Dockerfile",
                           "Сама площадка собирает Python и Node.js. Остальное — по своему "
                           "Dockerfile (`runtime: { kind: docker }`).",
                           "references/recipes-docker.md"))
    if info["kind"] == "unknown":
        out.append(Finding("no-stack", "change", "Не видно, на чём проект",
                           "Нет зависимостей Python, package.json и Dockerfile. Нужно понять "
                           "команду запуска и описать её в манифесте или Dockerfile.",
                           "references/manifest.md"))
    if info["has_manifest"]:
        out.append(Finding("has-manifest", "note", "looma.yaml уже есть",
                           "Проверить его: `looma validate`.", "references/manifest.md",
                           ["looma.yaml"]))
    return out


def secrets_of(project: Project) -> List[str]:
    """Имена переменных из примеров .env — кандидаты в `looma secrets set`."""
    names: List[str] = []
    for path, text in project.texts.items():
        base = path.rsplit("/", 1)[-1].lower()
        if not base.startswith(".env") and base not in ("env.example", "example.env"):
            continue
        for line in text.splitlines():
            found = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
            if found:
                names.append(found.group(1))
    keep = [n for n in dict.fromkeys(names)
            if n not in RESERVED_ENV and not n.startswith(RESERVED_PREFIXES)]
    return keep


def entrypoint(project: Project, info: dict) -> str:
    """Угадать команду веба. Это черновик: агент сверяет с README проекта."""
    if info["kind"] == "node":
        scripts = project.package.get("scripts") or {}
        if "start" in scripts:
            return "npm start"
        main = project.package.get("main")
        return f"node {main}" if main else "node index.js"
    if info["kind"] == "python":
        texts = project.texts
        if "manage.py" in texts:
            settings = re.search(r"['\"]([\w.]+)\.settings['\"]", texts["manage.py"])
            module = settings.group(1) if settings else "project"
            return f"gunicorn {module}.wsgi -b 0.0.0.0:$PORT"
        for path, text in sorted(texts.items()):
            if not path.endswith(".py") or auxiliary(path):
                continue
            module = path[:-3].replace("/", ".")
            if re.search(r"=\s*FastAPI\(", text):
                var = re.search(r"(\w+)\s*=\s*FastAPI\(", text).group(1)
                return f"uvicorn {module}:{var} --host 0.0.0.0 --port $PORT"
            if re.search(r"=\s*Flask\(", text):
                var = re.search(r"(\w+)\s*=\s*Flask\(", text).group(1)
                return f"gunicorn {module}:{var} -b 0.0.0.0:$PORT"
        if "streamlit" in project.py_deps:
            return "streamlit run app.py --server.port $PORT --server.address 0.0.0.0"
        for name in ("app.py", "main.py", "server.py", "run.py"):
            if name in texts:
                return f"python {name}"
    return ""


def manifest_draft(project: Project, info: dict, findings: List[Finding]) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", project.root.resolve().name.lower()).strip("-")[:40]
    name = name if re.match(r"^[a-z]", name or "") else f"app-{name}".strip("-")
    ids = {f.id for f in findings}
    lines = [f"name: {name or 'my-app'}"]
    if info["kind"] == "python":
        lines.append(f'runtime: {{ kind: python, python: "{info["python_version"] or "3.12"}" }}')
    elif info["kind"] == "node":
        lines.append(f'runtime: {{ kind: node, node: "{info["node_version"] or "22"}" }}')
    else:
        dockerfile = info["dockerfiles"][0] if info["dockerfiles"] else "Dockerfile"
        lines.append(f"runtime: {{ kind: docker, dockerfile: {dockerfile} }}")
    cmd = entrypoint(project, info) or "ЗАПОЛНИТЬ: команда запуска веба"
    lines += ["processes:", f'  web: {{ cmd: "{cmd}", port: 8080, min: 0 }}']
    if "background" in ids:
        lines.append('  # worker: { cmd: "ЗАПОЛНИТЬ: команда фонового процесса" }')
    data = []
    if ids & {"postgres", "sqlite", "mysql", "mongo", "redis", "broker", "search-engine"}:
        data.append("  postgres: {}")
    if ids & {"s3", "local-files"}:
        data.append("  bucket: {}")
    if data:
        lines += ["data:"] + data
    if ids & {"local-llm-server", "cuda", "heavy-ml", "external-llm-api", "speech"}:
        lines += ["gpu:", "  # ЗАПОЛНИТЬ по `looma models`: шаг: { model: <имя из каталога> }",
                  "  # chat: { model: <текстовая модель> }"]
    secrets = secrets_of(project)
    if secrets:
        lines.append(f"secrets: [{', '.join(secrets[:12])}]")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------- итог
ORDER = {"blocker": 0, "change": 1, "risk": 2, "note": 3}
LEVEL_NAMES = {"blocker": "Блокеры", "change": "Нужно переделать", "risk": "Проверить на staging",
               "note": "К сведению"}


def assess(root: Path) -> dict:
    project = walk(root)
    read_python_deps(project)
    read_node(project)
    read_imports(project)
    read_compose(project)
    info = stack(project)
    findings = apply_rules(project)
    for extra in (check_listen(project), check_outbound_ports(project),
                  check_extensions(project)):
        if extra:
            findings.append(extra)
    findings += check_compose(project) + check_size(project) + check_stack(project, info)
    findings.sort(key=lambda f: (ORDER[f.level], f.id))
    levels = {f.level for f in findings}
    if "blocker" in levels:
        verdict = ("blocked", "Упирается в ограничения площадки: без снятия блокеров не "
                              "заработает")
    elif levels & {"change", "risk"}:
        verdict = ("changes", "Подходит с правками")
    else:
        verdict = ("fits", "Подходит: нужен манифест")
    return {
        "project": {"path": str(root.resolve()), **info, "files": project.count,
                    "size_bytes": project.size,
                    "compose_services": {k: v.get("image", "build" if v.get("build") else "")
                                         for k, v in project.compose.items()}},
        "verdict": {"code": verdict[0], "text": verdict[1]},
        "findings": [asdict(f) for f in findings],
        "secrets": secrets_of(project),
        "manifest_draft": manifest_draft(project, info, findings),
    }


def markdown(report: dict) -> str:
    p = report["project"]
    out = [f"# Оценка проекта для Looma: {Path(p['path']).name}", "",
           f"**Итог: {report['verdict']['text']}.**", "",
           "## Проект", "",
           f"- Языки: {', '.join(p['languages']) or 'не определены'}",
           f"- Фреймворки: {', '.join(p['frameworks']) or 'не найдены'}",
           f"- Сборка на площадке: `{p['kind']}`" + (f" (Dockerfile: {', '.join(p['dockerfiles'])})"
                                                      if p["dockerfiles"] else ""),
           f"- Файлов {p['files']}, объём {_mb(p['size_bytes'])} (без служебных папок)"]
    if p["compose_services"]:
        services = ", ".join(f"{k} ({v})" if v else k for k, v in p["compose_services"].items())
        out.append(f"- Сервисы docker-compose: {services}")
    by_level: Dict[str, List[dict]] = {}
    for finding in report["findings"]:
        by_level.setdefault(finding["level"], []).append(finding)
    for level in ("blocker", "change", "risk", "note"):
        if level not in by_level:
            continue
        out += ["", f"## {LEVEL_NAMES[level]}", ""]
        for f in by_level[level]:
            out.append(f"### {f['title']}")
            out.append(f["detail"])
            if f["where"]:
                out.append("Где: " + ", ".join(f"`{w}`" for w in f["where"]))
            if f["fix"]:
                out.append(f"Как: {f['fix']}")
            out.append("")
    if report["secrets"]:
        out += ["## Секреты", "",
                "Из примеров .env — задать на площадке, не класть в код: "
                + ", ".join(f"`{s}`" for s in report["secrets"]), ""]
    out += ["## Черновик looma.yaml", "", "```yaml", report["manifest_draft"].rstrip(), "```", "",
            "Приметы найдены по тексту файлов: каждую проверить по указанным строкам."]
    return "\n".join(out) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    root = Path(args[0] if args else ".")
    if not root.is_dir():
        print(f"нет папки {root}", file=sys.stderr)
        return 2
    report = assess(root)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
