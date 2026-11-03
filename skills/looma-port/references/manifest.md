# looma.yaml

Один файл в корне проекта описывает приложение целиком. Платформа проверяет его при
каждом релизе и называет все ошибки сразу, с местом (`processes.web.port`). Без релиза —
`looma validate`.

```yaml
name: doc-helper                              # латиница в нижнем регистре, цифры, дефис; до 40
runtime: { kind: python, python: "3.12" }     # node: "22"; docker: { kind: docker, dockerfile: Dockerfile }
processes:
  web:     { cmd: "gunicorn app:app -b 0.0.0.0:$PORT", port: 8080, min: 0, idle_stop: 5m }
  worker:  { cmd: "python worker.py", memory: 1Gi, cpu: 1 }
  nightly: { schedule: "0 3 * * *", cmd: "python nightly.py", timeout: 1800 }
data:
  postgres: { extensions: [vector] }          # → DATABASE_URL
  bucket: {}                                  # → AWS_*, LOOMA_BUCKET
gpu:
  chat:  { model: Qwen3.8-Flash-Next-NVFP4 }  # имена — из `looma models`
  draw:  { model: Qwen-Image-2.1 }
secrets: [STRIPE_KEY, SENTRY_DSN]
events:
  - answer
  - { name: picture, title: Картинка }
access: clients
channels:
  api: { docs: /docs }
  mcp:
    tools:
      - name: ask
        description: Ответ на вопрос по загруженным документам, со ссылками на источник
        path: /api/ask
        input: { type: object, properties: { question: { type: string } }, required: [question] }
listing:
  title: Помощник по документам
  summary: Ответы по PDF со ссылками на источник
  description: |
    Абзац — одной строкой, между абзацами пустая строка: витрина сохраняет переносы.
  category: business
  tags: [документы, поиск]
  icon: icon.png
```

## runtime

| Поле | Значение |
|---|---|
| `kind` | `python`, `node` или `docker` |
| `python` | версия образа `python:<версия>-slim`, по умолчанию `"3.12"` |
| `node` | версия образа `node:<версия>-slim`, по умолчанию `"22"` |
| `dockerfile` | для `docker`: путь внутри проекта, по умолчанию `Dockerfile` |

Для `python` платформа пишет Dockerfile сама: `WORKDIR /app`, `pip install -r
requirements.txt` (или `pip install .` по `pyproject.toml`), код в `/app`. Для `node`:
`npm ci` (есть `package-lock.json`) или `npm install`, затем `npm run build --if-present`,
`NODE_ENV=production`. Команду запуска (CMD) образ не задаёт: она — в `processes`.

## processes

Вид процесса — по полям: есть `port` — веб (один на приложение), есть `schedule` — по
расписанию, иначе — фоновый (`worker`). Имя процесса — латиница, цифры, дефис.

| Поле | Что | По умолчанию |
|---|---|---|
| `cmd` | команда запуска, выполняется через `/bin/sh -c`: `$PORT` и другие переменные раскрываются | обязательно (у `docker` может дать образ) |
| `port` | порт веба, 1024–65535; процесс слушает `0.0.0.0` на нём (он же в `$PORT`) | — |
| `schedule` | cron из пяти полей, время UTC | — |
| `min`, `max` | экземпляров 0–10; `min: 0` — веб спит без запросов; `max: 0` — выключен | 1 и `max(1, min)` |
| `idle_stop` | только веб: через сколько без запросов уснуть (`300s`, `5m`, `1h`; от 60 с до суток) | 5m |
| `cpu` | доля ядер 0.1–8 | 0.5 |
| `memory` | `64Mi`–`32Gi` (`512Mi`, `1Gi`) | 512Mi |
| `timeout` | для задачи по расписанию: секунд на прогон | — |

Рабочая папка — `/app` (у `docker` — `WORKDIR` образа). На staging запускается только
веб; `worker` и задачи по расписанию — только на prod. Порт: проще всего слушать
`$PORT` и написать в `port` любой из диапазона (например, 8080).

## data

- `postgres: { extensions: [...] }` — своя база, строка подключения в `DATABASE_URL`.
  Расширения — только из списка: `vector`, `pg_trgm`, `uuid-ossp`, `citext`, `hstore`,
  `pgcrypto`, `btree_gin`, `btree_gist`, `unaccent`, `fuzzystrmatch`.
- `bucket: {}` — свой S3-бакет: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `LOOMA_BUCKET`. Адресация — по пути (path-style).

Таблицы и миграции — забота приложения: прогонять миграции при старте веба или
отдельной командой перед ним (`cmd: "python manage.py migrate && gunicorn …"`).

## gpu

Шаги с моделями, которые приложение вызывает: `<шаг>: { model: <имя> }`, по желанию
`adapter: <имя>` (дообученный адаптер). Имя шага — латиница, цифры, дефис; имя модели —
ровно как в `looma models`. Токен приложения пускает только к моделям отсюда, остальные
отвечают 403.

## secrets

Список имён (A–Z, цифры, `_`). Значения задаёт автор: `looma secrets set ИМЯ=значение`;
процессы видят их после перезапуска. Имена платформы (`PORT`, `DATABASE_URL`, `*_PROXY`,
`LOOMA_*`, `AWS_*`, `S3_*` и др.) заняты.

## events

События для учёта по клиентам маркетплейса (`charge`): имя (латиница, цифры, `_`, `-`)
или `{ name, title }`. До 20 событий.

## access

`public` (по умолчанию) — открыто всем; `clients` — только вошедшим на витрине
маркетплейса и программам с ключом клиента. Каждый запрос несёт `X-Looma-Client`.

## channels

- `api: { docs: /путь }` — объявить программный доступ для карточки (ключ клиента
  `looma_ck_…` в `Authorization: Bearer`).
- `mcp.tools` — инструменты MCP-сервера платформы (`/mcp/<имя>`), до 20: `name`,
  `description` (по ней агент решает, звать ли инструмент, — писать конкретно, до 1000
  знаков), необязательный `title`, `path` (POST сюда, тело — аргументы JSON) и `input`
  (JSON Schema с `type: object`). Вызовы идут на prod.

## listing

Карточка в каталоге маркетплейса; `looma publish` берёт её из релиза на prod.

| Поле | Предел |
|---|---|
| `title` | 2–60 знаков |
| `summary` | одна строка, до 160 |
| `description` | до 4000; абзац — одной строкой |
| `category` | `assistants`, `images`, `video`, `text`, `code`, `data`, `business`, `education`, `fun`, `other` |
| `tags` | до 8, каждый до 24 знаков |
| `icon` | PNG, JPEG или WebP в проекте, до 256 КБ (лучше 256×256) |

## .loomaignore

Что не класть в архив релиза — по строке на шаблон, как в `.gitignore` без отрицаний:
`data/`, `*.log`, `tests/fixtures/big`. Уже исключены: `.git`, `node_modules`, `.venv`,
`venv`, `__pycache__`, кэши, `.env`, `.env.*`, `.next`, `.idea`, `.vscode`. Ссылки
(symlink) в архив не идут.
