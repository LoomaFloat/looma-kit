# looma.yaml

Манифест `looma.yaml` лежит в корне проекта и описывает сервис целиком. Площадка
проверяет его при каждом релизе и называет все ошибки сразу, с местом
(`processes.web.port`). Проверить манифест без релиза: `looma validate`.

```yaml
name: doc-helper                              # латиница в нижнем регистре, цифры, дефис; до 40
runtime: { kind: python, python: "3.12" }     # node: "22"; docker: { kind: docker, dockerfile: Dockerfile }
processes:
  web:     { cmd: "gunicorn app:app -b 0.0.0.0:$PORT", port: 8080, min: 0, idle_stop: 5m }
  worker:  { cmd: "python worker.py", memory: 1Gi, cpu: 1 }
  nightly: { schedule: "0 3 * * *", cmd: "python nightly.py", timeout: 1800 }
data:
  postgres: { extensions: [vector] }          # строка подключения в DATABASE_URL
  bucket: {}                                  # доступы в AWS_*, имя в LOOMA_BUCKET
gpu:
  chat:  { model: Qwen3.8-Flash-Next-NVFP4 }  # имена моделей из `looma models`
  draw:  { model: Qwen-Image-2.1 }
secrets: [STRIPE_KEY, SENTRY_DSN]
events:
  - answer                                         # бесплатно
  - { name: picture, title: Изображение, price: 5 } # цена в рублях
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
    Абзац пишется одной строкой, между абзацами пустая строка: витрина сохраняет переносы.
  category: business
  tags: [документы, поиск]
  icon: icon.png
```

## name

Имя сервиса: латиница в нижнем регистре, цифры и дефис, до 40 знаков, начинается с
буквы. Из имени получаются адреса `имя.apps.loomafloat.ru` (prod) и
`имя-staging.apps.loomafloat.ru` (staging).

## runtime

| Поле | Значение |
|---|---|
| `kind` | `python`, `node` или `docker` |
| `python` | версия образа `python:<версия>-slim`, по умолчанию `"3.12"` |
| `node` | версия образа `node:<версия>-slim`, по умолчанию `"22"` |
| `dockerfile` | для `docker`: путь внутри проекта, по умолчанию `Dockerfile` |

Для `python` площадка пишет Dockerfile сама: `WORKDIR /app`, `pip install -r
requirements.txt` (или `pip install .` по `pyproject.toml`), код в `/app`. Для `node`:
`npm ci` (если есть `package-lock.json`) или `npm install`, затем
`npm run build --if-present`, `NODE_ENV=production`. Команду запуска (CMD) образ не
задаёт: она указывается в `processes`.

## processes

Вид процесса определяется полями: с `port` это веб (один на сервис), с `schedule`
задача по расписанию, иначе фоновый процесс (`worker`). Имя процесса: латиница в
нижнем регистре, цифры, дефис.

| Поле | Что | По умолчанию |
|---|---|---|
| `cmd` | команда запуска через `/bin/sh -c`, переменные вроде `$PORT` раскрываются | обязательно (у `docker` её может дать образ) |
| `port` | порт веба, 1024–65535; процесс слушает `0.0.0.0` на этом порту, номер есть и в `$PORT` | — |
| `schedule` | cron из пяти полей, время UTC | — |
| `min`, `max` | экземпляров 0–10; с `min: 0` веб спит без запросов; `max: 0` выключает процесс | 1 и `max(1, min)` |
| `idle_stop` | только веб: через сколько без запросов уснуть (`300s`, `5m`, `1h`; от 60 с до суток) | `5m` |
| `cpu` | доля ядер 0.1–8 | 0.5 |
| `memory` | `64Mi`–`32Gi`, например `512Mi`, `1Gi` | `512Mi` |
| `timeout` | только для задачи по расписанию: сколько секунд может идти один запуск | 3600 |

Рабочая папка `/app` (у `docker` это `WORKDIR` образа). На staging запускается только
веб, процессы `worker` и задачи по расписанию работают только на prod.

Порт проще всего не выбирать: слушайте `$PORT`, а в `port` укажите любой номер из
диапазона, например 8080.

## data

- `postgres: { extensions: [...] }`: своя база, строка подключения в `DATABASE_URL`.
  Расширения только из списка: `vector`, `pg_trgm`, `uuid-ossp`, `citext`, `hstore`,
  `pgcrypto`, `btree_gin`, `btree_gist`, `unaccent`, `fuzzystrmatch`.
- `bucket: {}`: свой S3-бакет. Доступы в `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
  `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, имя в `LOOMA_BUCKET`. Адресация по пути
  (path-style).

Таблицы и миграции создаёт сам сервис: при старте веба или отдельной командой перед
ним, например `cmd: "python manage.py migrate && gunicorn …"`. Подробнее:
[Данные](data.md).

## gpu

Модели, которые вызывает сервис: `<шаг>: { model: <имя> }`, по желанию
`adapter: <имя>` (дообученный адаптер). Имя шага: латиница в нижнем регистре, цифры,
дефис. Имя модели берётся из `looma models` как есть. Токен сервиса пускает только к
моделям отсюда, остальные отвечают `403`. Подробнее: [Модели](models.md).

## secrets

Список имён: заглавная латиница, цифры и `_`, до 64 знаков, не с цифры. Значения
задаёте вы: `looma secrets set ИМЯ=значение`. Процессы видят их после перезапуска.
Имена площадки заняты: [Занятые имена](env.md#reserved).

## events

События для `charge` и их цены, до 20. Событие задаётся именем (`answer`) или словарём
`{ name, title, note, price }`.

| Поле | Что |
|---|---|
| `name` | латиница в нижнем регистре, цифры, `_` и `-`, до 40 знаков, начинается с буквы |
| `title` | подпись для клиента («Ролик»), одна строка до 60 знаков |
| `note` | пояснение к цене («по одной фразе, со звуком»), одна строка до 80 знаков |
| `price` | цена одного события в рублях, от 0 до 100 000, до копеек: `49`, `2.5`; без цены событие бесплатное |

Клиент видит цены на карточке. В `charge` действует цена одобренной карточки: новая
цена начинает действовать после `looma publish` и модерации, а не после нового
релиза. С `quantity: 2` в `charge` записываются две цены.

## access

`public` (по умолчанию) открывает сервис всем. `clients` пускает только клиентов,
вошедших на витрине, и программы с ключом клиента. С `clients` каждый запрос несёт
заголовок `X-Looma-Client`.

## channels

- `api: { docs: /путь }`: объявляет для карточки программный доступ по ключу клиента
  `looma_ck_…` в `Authorization: Bearer`. В `docs` путь к описанию API.
- `mcp.tools`: инструменты MCP-сервера площадки (`/mcp/<имя>`), до 20. Поля: `name`
  (латиница, цифры, `_` и `-`, до 64 знаков), `description`, необязательный `title`
  (до 60 знаков), `path` (сюда приходит POST с аргументами в JSON) и `input` (JSON
  Schema с `type: object`, до 16 КБ). По `description` ИИ-агент решает, звать ли
  инструмент, поэтому пишите конкретно, до 1000 знаков. Вызовы идут на prod.

## listing

Карточка в каталоге. `looma publish` берёт её из релиза на prod.

| Поле | Предел |
|---|---|
| `title` | 2–60 знаков |
| `summary` | одна строка, до 160 знаков |
| `description` | до 4000 знаков; абзац пишется одной строкой, между абзацами пустая строка |
| `category` | `assistants`, `images`, `video`, `text`, `code`, `data`, `business`, `education`, `fun`, `other` (по умолчанию) |
| `tags` | до 8, каждый до 24 знаков |
| `icon` | PNG, JPEG или WebP в проекте, до 256 КБ (лучше 256×256) |

## .loomaignore

Что не класть в архив релиза: по строке на шаблон, как в `.gitignore`, без отрицаний.
Например `data/`, `*.log`, `tests/fixtures/big`. Уже исключены `.git`,
`node_modules`, `.venv`, `venv`, `__pycache__`, кэши, `.env`, `.env.*`, `.next`,
`.idea`, `.vscode`. Символьные ссылки (symlink) в архив не идут.
