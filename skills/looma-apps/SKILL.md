---
name: looma-apps
description: Приложения на площадке Looma - деплой, staging и prod, логи, откат, секреты, модели, статистика, публикация в маркетплейсе. Для проектов, где уже есть looma.yaml.
license: MIT
compatibility: Нужны CLI looma (github.com/LoomaFloat/looma-kit) и вход `looma login`; MCP-сервер `looma mcp` по желанию.
metadata:
  version: "0.2.0"
  homepage: "https://github.com/LoomaFloat/looma-kit"
---

# Приложения на Looma

Looma хостит приложения авторов с GPU-моделями. У приложения два окружения:
**staging** (каждый новый релиз встаёт сюда сам) и **prod** (только переводом).
Адреса: `https://<имя>-staging.apps.loomafloat.ru` и `https://<имя>.apps.loomafloat.ru`.

Работать через MCP-инструменты `looma_*` (если сервер подключён), иначе — CLI `looma`
в терминале (`looma status`, `looma deploy`, …): у них одни и те же действия.

## Правило: проверь перед записью, проверь после записи

1. **Перед** `deploy`, `promote`, `rollback`, `stop`, `secrets set` — `looma_status`
   (`looma status`): что запущено, на каком релизе, есть ли ошибки. Перед `deploy` —
   ещё `looma validate`: ошибки манифеста и модели из `gpu:` за секунды, без релиза.
2. **После** `deploy` — `looma_check` staging (`looma check --env staging`); после
   `promote`/`rollback` — prod. Проверка идёт через API владельца и доходит до процесса
   и при `access: clients`. Не 2xx — `looma_logs` нужного процесса и окружения и только
   потом выводы.
3. На prod — когда staging проверен или пользователь просит явно.
   `destroy` — только по прямой просьбе: уходят база, бакет и релизы.
4. Значения секретов не повторять в ответах и не писать в файлы проекта — только
   `looma_secrets_set`; процессы видят их после `looma_restart`.

## Манифест looma.yaml

```yaml
name: my-app                      # латиница, цифры, дефис
runtime: { kind: python, python: "3.12" }    # node: "22"; docker: { dockerfile: Dockerfile }
processes:
  web:    { cmd: "python app.py", port: 8080, min: 0, max: 1, idle_stop: 10m }  # слушать 0.0.0.0:$PORT
  worker: { cmd: "python worker.py", memory: 512Mi, cpu: 0.5 }
  nightly: { schedule: "0 3 * * *", cmd: "python nightly.py" }              # cron, UTC
data:
  postgres: { extensions: [vector] }   # → DATABASE_URL
  bucket: {}                           # → AWS_* и LOOMA_BUCKET (адресация S3 по пути)
gpu:
  chat: { model: Qwen3.8-Flash-Next-NVFP4 }    # имена — как в /v1/models
secrets: [TELEGRAM_BOT_TOKEN]
events: [answer]                       # события для charge (учёт по клиентам)
access: clients                        # только клиентам маркетплейса (по умолчанию public)
listing:                               # карточка в маркетплейсе
  title: Студия картинок
  summary: Одной строкой — что делает
  category: images     # assistants, images, video, text, code, data, business, education, fun, other
  tags: [картинки]
  icon: icon.png       # PNG, JPEG или WebP до 256 КБ (необязательно)
```

- `min: 0` — веб спит без запросов и просыпается первым запросом (до двух минут).
- На staging запускается только веб; worker и cron — только на prod.
- Процесс получает `LOOMA_APP_TOKEN`, `LOOMA_API_URL`, `LOOMA_APP_URL`,
  `LOOMA_ENVIRONMENT`, `LOOMA_RELEASE`, данные и секреты; в интернет — через прокси.
- Модели приложение зовёт своим токеном (OpenAI-совместимый API) — только те,
  что в `gpu:`, иначе 403. В Python: `from loomafloat import Looma; Looma().chat(...)`.
  Какие модели есть, спят ли — `looma models` (`looma_models`).

## Маркетплейс

`looma_publish` (`looma publish`) — карточку из раздела `listing` релиза на **prod** — на
модерацию; `looma_listing` — в каталоге ли и почему отказ. Публиковать — только по
просьбе пользователя: карточку увидят все. Поменять карточку — правка `listing`,
`deploy`, `promote`, снова `publish`.

## Клиенты маркетплейса

Клиента платформа называет в запросе к приложению заголовком `X-Looma-Client`
(`client_of(headers)` в SDK): псевдоним без почты и имени. Вызов модели для него —
`looma.for_client(client).chat(…)`, событие для учёта — `looma.charge("answer", client)`;
имя события объявить в `events:` манифеста, иначе 400. Без клиента (`None`) — не
вызывать `charge`.

`access: clients` — посетителя без входа шлюз сам отправляет на вход в маркетплейс
(страница со ссылкой, для API — 401), в приложение доходят только клиенты. Ставить,
когда вызов дорогой (видео, большие модели) или у каждого должны быть свои данные.

## Статистика

`looma_stats` (`looma stats`) — за сколько дней: запросы по каналам, клиенты, события
charge, вызовы моделей и себестоимость по текущему прайсу (оценка, не счёт). Вызовы
`looma dev` — отдельно.

## Каналы

`channels:` в looma.yaml — как клиент доберётся кроме веба: `api: {}` (по ключу клиента),
`mcp: { tools: [{ name, description, path, input }] }` (инструменты для агентов: POST на
`path`, ответ — JSON, текст или картинка), `telegram: { webhook: /telegram, bot: "@Бот" }`
(обновления через платформу, отправитель — в `X-Looma-Client`; вебхук бот ставит на
`LOOMA_TELEGRAM_WEBHOOK_URL` с `secret_token=LOOMA_TELEGRAM_SECRET`). Описание
инструмента — по нему агент решает, звать ли его: писать конкретно.

## У себя: `looma dev`

Запустить приложение на машине пользователя — `looma dev` в терминале (процесс
долгий, Ctrl+C — стоп; MCP-инструмента для него нет). Окружение — как на платформе
(`LOOMA_ENVIRONMENT=dev`), база и бакет — в Docker, модели — настоящие через
dev-токен приложения (только из `gpu:`, 200 вызовов в сутки). Секреты — строками
в `.env` проекта, в релиз он не уезжает. `looma dev run <процесс>` — один запуск
(cron), `looma dev --docker` — в образе, как на платформе, `looma dev down` — стоп.
Проверить код до деплоя — так дешевле, чем релизом.

## Перенос чужого проекта

Проект без `looma.yaml`, который надо перенести на площадку, — skill `looma-port`
(оценка, план, переделка, деплой, отчёт).

## Шаблоны

`looma_init` / `looma init <папка> -t <шаблон>`: `hello`, `assistant` (чат на большой
модели), `telegram-bot` (картинки, правка фото, видео; токен — секретом
`TELEGRAM_BOT_TOKEN`), `image-studio` (генерация, правка и галерея в бакете — у каждого
клиента своя), `video-studio` (ролик по описанию: задание на платформе, готовый — в
бакет, событие `video`). Шаблоны со SDK кладут его копию в `loomafloat/`; обновить её
после обновления devkit — `looma sdk` в папке проекта.

## Частые причины

- **Сборка не удалась** — причина и хвост лога в результате `looma_deploy`
  (`log_tail`): зависимости в `requirements.txt`/`package.json`, ошибка манифеста.
- **403 от модели** — модели нет в `gpu:` манифеста или имя не совпадает с `/v1/models`
  (в `looma dev` — после правки `gpu:` перезапустить `looma dev`).
- **429 `dev_quota_exceeded`** в `looma dev` — дневная квота dev-вызовов приложения
  выбрана; обновится в 00:00 UTC, на staging и prod она не действует.
- **503 на адресе** — окружение остановлено или процесс падает: `looma_status`, `looma_logs`.
- **Процесс падает и перезапускается** — `restarts` и `last_exit` в `looma_status`,
  сохранённый лог — `looma_logs`.
