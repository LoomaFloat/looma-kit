# loomafloat — приложения на Looma из терминала и из кода

CLI `looma`: от исходников до прода. SDK: вызовы моделей из кода приложения.
Ни одной зависимости — нужен только Python 3.10+.

```bash
uv tool install "git+https://github.com/LoomaFloat/looma-kit"
# или из клона: uv tool install .
```

## Путь автора

```bash
looma login                 # откроет консоль: подтвердите вход — ключ придёт сам
looma init my-bot           # заготовка: looma.yaml + app.py
cd my-bot
looma dev                   # у себя: http://127.0.0.1:8080, база и бакет — в Docker
looma deploy                # собрать релиз — встанет на staging
looma open --env staging    # https://my-bot-staging.apps.loomafloat.ru/
looma promote               # staging → prod: https://my-bot.apps.loomafloat.ru/
looma publish               # карточка из раздела listing — в маркетплейс, через модерацию
```

| команда | что |
|---|---|
| `looma login [--key К \| --paste] [--no-browser]` · `logout` · `whoami` | вход через браузер: код в терминале, подтверждение в консоли (страница показывает машину и адрес), ключ проекта выпускается сам; `--key`/`--paste` — готовым ключом со страницы «Ключи». Ключ — в `~/.config/looma/config.json` (0600) |
| `looma init [папка] [-t hello]` | заготовка приложения |
| `looma sdk [папка]` | свежая копия SDK в `loomafloat/` проекта — после обновления devkit |
| `looma dev [run процесс \| env \| down] [--docker] [--no-data]` | запустить у себя с окружением платформы (ниже) |
| `looma assess [папка] [--json] [--out файл]` | оценка проекта: подойдёт ли площадке, что мешает, что переделать (файл:строка), черновик looma.yaml; ключ не нужен |
| `looma validate [папка]` | проверка looma.yaml и проекта без релиза: все ошибки манифеста, модели из `gpu:` против каталога, иконка, Dockerfile, архив |
| `looma models` | модели каталога: имя для `gpu:`, тип, отвечает или спит, контекст |
| `looma check [--env staging] [--path /] [--public]` | запрос к вебу приложения: код, время, начало ответа; по умолчанию через API владельца (доходит и при `access: clients`) |
| `looma skill install [--agent claude\|cursor\|codex\|copilot\|gemini\|agents] [--project [папка]]` · `looma skill path` | skills Looma для ИИ-агентов — туда, где их читает агент |
| `looma deploy [папка] [--promote] [--no-wait]` | упаковать проект, собрать релиз, поставить на staging (и на prod); не собралось — хвост лога сборки |
| `looma status` | окружения, адреса, процессы, сертификат |
| `looma logs [процесс] [--env] [-f]` | лог процесса (по умолчанию — web на prod) |
| `looma promote [--release N]` | на prod — релиз staging или названный |
| `looma rollback [--env]` | окружение — на прошлый релиз |
| `looma stop [--env]` · `restart процесс` | остановить; перезапустить (cron — запустить сейчас) |
| `looma secrets [set ИМЯ=значение … \| unset ИМЯ …]` | секреты процессов |
| `looma releases` · `apps` · `open` · `destroy --yes` | релизы, приложения, адрес, удаление |
| `looma stats [--days 30]` | статистика: запросы по каналам, клиенты, события, вызовы моделей, себестоимость по прайсу |
| `looma publish` · `listing` · `unpublish` | карточка в маркетплейсе: опубликовать (из релиза на prod, через модерацию), посмотреть решение, снять |

## У себя: `looma dev`

```bash
looma dev                 # web и worker здесь, база и бакет — в Docker, Ctrl+C — стоп
looma dev --docker        # процессы — в образе, собранном как на платформе
looma dev run nightly     # процесс один раз (cron — вне расписания)
eval "$(looma dev env)"   # переменные процесса — в свой терминал или IDE
looma dev down [--wipe]   # остановить базу и бакет (--wipe — удалить с данными)
```

Процесс получает то же, что на платформе: `PORT`, `DATABASE_URL` и `PG*`, `AWS_*` и
`LOOMA_BUCKET`, `LOOMA_APP_TOKEN` и `LOOMA_API_URL`; `LOOMA_ENVIRONMENT` — `dev`.

- **Модели** — настоящие, через dev-токен приложения: только объявленные в `gpu:`,
  не больше 200 вызовов в сутки на приложение, свой лимит в минуту — работающее
  приложение отладка не задевает. Манифест проверяет сервер: ошибки те же, что у `deploy`.
- **База** — Postgres с pgvector, **бакет** — SeaweedFS (S3): те же образы, что у
  платформы; контейнеры `looma-dev-<имя>-postgres` и `-s3`, порты только на 127.0.0.1,
  данные — в томах Docker и переживают перезапуск.
- **Секреты** — из `.env` в папке проекта (в релиз он не уезжает); с платформы значения
  не приходят. Ваш `LOOMA_API_KEY` и свои `AWS_*` процесс не видит — как на платформе.
- Без `--docker` процессы идут на вашей машине: зависимости ставьте сами
  (`pip install -r requirements.txt`). `runtime: docker` всегда идёт в образе.
- Приложения на платформе ещё нет — `looma dev` заведёт его (только имя, без релизов).

## Клиенты маркетплейса

Клиент, вошедший на витрине (или пришедший с ключом клиента), виден приложению
заголовком `X-Looma-Client` — псевдонимом, без почты и имени:

```python
from loomafloat import Looma, client_of

looma = Looma()
client = client_of(self.headers)            # None — пришёл не клиент маркетплейса
text = looma.for_client(client).chat(MODEL, prompt)   # вызов записан на клиента
looma.charge("answer", client)              # событие из раздела events looma.yaml
```

```yaml
events:
  - { name: answer, title: Ответ }
```

`charge` — технический учёт (денег пока не двигает); `id="…"` — повтор не
запишется дважды. Шаблон `assistant` так и делает.

Приложение только для клиентов — `access: clients` в `looma.yaml`: посетителя без
входа шлюз сам отправляет войти в маркетплейс (страница со ссылкой, для API — 401),
и в запросах через шлюз `client_of` не `None`. Витрина показывает в карточке «Доступ:
после входа». По умолчанию `access: public` — открыто всем.

Каналы кроме веба — раздел `channels` (видны в карточке маркетплейса):

```yaml
channels:
  api: { docs: /docs }                     # API по ключу клиента
  mcp:                                     # инструменты для Claude, Cursor и других агентов
    tools:
      - { name: ask, description: Ответить на вопрос, path: /api/ask,
          input: { type: object, properties: { question: { type: string } }, required: [question] } }
  telegram: { webhook: /telegram, bot: "@MyBot" }   # обновления бота — через платформу
```

MCP-сервер приложения — `https://api.loomafloat.ru/mcp/<имя>`, с ключом клиента;
вызов инструмента — POST на его `path` с `X-Looma-Client`. Бот с `webhook` ставит
вебхук на `LOOMA_TELEGRAM_WEBHOOK_URL` с секретом `LOOMA_TELEGRAM_SECRET` и
получает отправителя в `X-Looma-Client` (шаблон `telegram-bot`).

## Шаблоны `looma init -t …`

| шаблон | что внутри |
|---|---|
| `hello` | страница, которая знает своё окружение и релиз |
| `assistant` | чат на большой модели: ответ печатается по мере генерации, характер — секретом `ASSISTANT_SYSTEM` |
| `telegram-bot` | бот: текст → картинка, фото с подписью → правка фото, `/video` → ролик; вебхук ставит сам, только на prod |
| `image-studio` | генерация и правка картинок, галерея в бакете приложения (у каждого клиента своя), инструмент MCP `draw` |
| `video-studio` | ролик на 5 секунд по описанию; задание считает платформа, готовый ролик — в бакете, событие `video` |

Шаблонам с моделями `looma init` кладёт в проект копию SDK (`loomafloat/`): сборка на
платформе не ходит за ним в PyPI. Обновить копию после обновления devkit — `looma sdk`
(заменяет только модули SDK). Модели — в `gpu:` манифеста; другая модель — поменять
там и в коде (или секретом, как написано в README шаблона).

Приложение — из `--app`, иначе имя из `looma.yaml` текущей папки. Не уезжают в
релиз `.git`, `node_modules`, виртуальные окружения, кэши, `.env` и то, что
перечислено в `.loomaignore`.

Переменные окружения сильнее файла настроек: `LOOMA_API_KEY`, `LOOMA_API_URL`,
`LOOMA_CONFIG`.

## Из Claude Code, Cursor и других агентов (MCP)

```bash
looma login                              # ключ — общий с CLI
claude mcp add looma -- looma mcp        # Claude Code
looma mcp --install-skill                # skill looma-apps: манифест, правила, частые причины
looma mcp --print-config                 # как подключить Cursor и других
```

Инструменты: `looma_status`, `looma_init`, `looma_deploy` (ждёт сборки, при отказе —
причина и хвост лога), `looma_check` (запрос к адресу приложения), `looma_logs`,
`looma_promote`, `looma_rollback`, `looma_stop`, `looma_restart`, `looma_secrets_list`,
`looma_secrets_set`, `looma_releases`, `looma_apps`, `looma_whoami`, `looma_stats`, `looma_publish`,
`looma_listing`, `looma_unpublish`, `looma_destroy` (с подтверждением именем). Агенту сервер сразу даёт правило: проверь перед записью
(`looma_status`), проверь после записи (`looma_check`, при ошибке — `looma_logs`).

## SDK в коде приложения

Платформа кладёт процессу `LOOMA_APP_TOKEN` и `LOOMA_API_URL` — `Looma()`
находит их сам. Модели — только те, что объявлены в `gpu:` манифеста.

```python
from loomafloat import Looma

looma = Looma()
text = looma.chat("Qwen3.8-Flash-Next-NVFP4", "Придумай имя коту")
png = looma.image("Qwen-Image-2.1", "рыжий кот в скафандре", size="1024x1024")[0]
edit = looma.image("Qwen-Image-2.1", "надень на кота шляпу", images=["cat.png"])[0]
mp4 = looma.video("MiniMax-H3", "A red fox in a snowy forest", quality="fast").content()
```

`requirements.txt` приложения: `loomafloat @ git+https://github.com/LoomaFloat/looma-kit`.

Ошибка API — `LoomaError` с кодом и причиной словами (`exc.status`, `exc.message`).
