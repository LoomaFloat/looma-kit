# MCP-сервер looma

`looma mcp` запускает MCP-сервер для ИИ-агента автора: Claude Code, Cursor, Codex и
других. Через него агент выпускает сервис, проверяет его, читает логи и отправляет
карточку на модерацию. Это те же действия, что у [CLI](cli.md), в виде 32 инструментов.

Сервер работает по stdio: в stdout идут только сообщения протокола, остальное в stderr.

## Подключение

Ключ общий с CLI: сначала выполните `looma login` или задайте `LOOMA_API_KEY` в
окружении сервера.

```sh
looma login
claude mcp add --transport stdio --scope user looma -- looma mcp   # Claude Code
looma mcp --print-config                                           # настройки для других агентов
```

`looma mcp --print-config` печатает готовые настройки MCP для популярных агентов:

| Агент | Как подключить |
|---|---|
| Claude Code | `claude mcp add --transport stdio --scope user looma -- looma mcp` |
| OpenAI Codex | `codex mcp add looma -- looma mcp` |
| GitHub Copilot CLI | `copilot mcp add looma -- looma mcp` |
| Gemini CLI | `gemini mcp add -s user looma looma mcp` |
| Cursor, Claude Code в проекте | `~/.cursor/mcp.json` или `.mcp.json` проекта: `{"mcpServers": {"looma": {"command": "looma", "args": ["mcp"]}}}` |
| VS Code | `.vscode/mcp.json`: `{"servers": {"looma": {"type": "stdio", "command": "looma", "args": ["mcp"]}}}` |

Skills для агента ставит `looma skill install --agent <агент>`, для Claude Code также
`looma mcp --install-skill`. Подробно о подключении агентов:
[Перенос проекта с ИИ-агентом](https://docs.loomafloat.ru/authors/port/).

## Правило для агента

Сервер сразу даёт агенту правило: проверить состояние перед изменением и проверить
результат после.

1. Перед `looma_deploy`, `looma_promote`, `looma_rollback`, `looma_stop` и
   `looma_secrets_set` агент вызывает `looma_status`.
2. После `looma_deploy` агент проверяет staging через `looma_check`, после
   `looma_promote` и `looma_rollback` проверяет prod. При ошибке читает `looma_logs`
   нужного процесса и окружения.
3. На prod агент переводит, только когда staging проверен или пользователь просит явно.
   `looma_destroy` вызывается только по прямой просьбе пользователя.
4. Значения секретов агент не пересказывает и не пишет в файлы проекта, только
   передаёт в `looma_secrets_set`.
5. `looma_model_publish` и `looma_model_delete` вызываются только по прямой просьбе
   пользователя: первую модель увидят все авторы, второе необратимо. То же с
   `looma_engine_submit` и `looma_engine_share`: код движка уходит на ревью и к другим
   авторам.
6. Чужой проект агент переносит по skill `looma-port`: `looma_assess`, план
   пользователю, правки, `looma_validate`, `looma_deploy`, `looma_check`, отчёт. Модели
   для `gpu` он берёт из `looma_models`.

## Инструменты

Аргумент `app` везде означает имя сервиса, как в `looma.yaml`, или его id. `environment`
принимает `prod` или `staging`.

| Инструмент | Аргументы | Что делает |
|---|---|---|
| `looma_whoami` | | кто вошёл: учётная запись, проект, права ключа |
| `looma_apps` | | ваши сервисы с релизами окружений и адресами |
| `looma_status` | `app` | состояние сервиса: окружения (релиз, состояние, адрес), процессы, сертификат, последние переключения; вызывается перед любым изменением |
| `looma_init` | `directory`, `template`, `name` | создать проект сервиса из шаблона в папке на диске |
| `looma_assess` | `directory` | оценить проект: подойдёт ли площадке, что мешает, что переделать (с местами `файл:строка`), черновик `looma.yaml`; ключ не нужен |
| `looma_validate` | `directory` | проверить `looma.yaml` и проект без релиза: все ошибки манифеста сразу, модели из `gpu` (есть ли в каталоге, спят ли), иконка, Dockerfile, архив; секунды вместо сборки |
| `looma_models` | | модели каталога: имя для `gpu`, тип (`text`, `image`, `video`, `embedding`, `rerank`), отвечает или спит, контекст |
| `looma_model_list` | | свои модели автора в каталоге: имя `автор/модель`, вид (веса, адаптер, настройка), состояние, публикация, цена в копейках; `handle` показывает имя автора |
| `looma_model_check` | `repo`, `engine`, `recipe` | проверить веса с HuggingFace без запуска: формат, размер, встанет ли модель в сети. С движком автора (`engine` вида `автор/движок`) проверяет, есть ли узлы под движок |
| `looma_model_add` | `name`, одно из: `repo`; `base` и `adapter`; `base` и `preset`. Ещё `engine`, `recipe`, `summary`, `always`, `handle` | добавить свою модель: свои веса, модель каталога с адаптером LoRA или настройку поверх модели каталога. `handle` нужен один раз, пока имя автора не задано |
| `looma_model_set` | `name`, `summary`, `price`, `preset`, `always` | поменять описание, цену в копейках (`{}` убирает цену), настройку или реплики |
| `looma_model_publish` | `name` | подать модель на проверку площадки; только по просьбе пользователя |
| `looma_model_unpublish` | `name` | снять модель с публикации или отозвать заявку |
| `looma_model_delete` | `name`, `confirm` | удалить модель. Необратимо: только по прямой просьбе пользователя, в `confirm` ещё раз полное имя |
| `looma_engine_list` | | свои движки и расширения: имя `автор/движок`, открыт ли авторам, версии с решением ревью и причиной отказа |
| `looma_engine_submit` | `repo`, `commit`, `path`, `extension` | подать версию движка или расширения на ревью: репозиторий на GitHub и коммит целиком; только по просьбе пользователя |
| `looma_engine_share` | `name`, `public` | открыть одобренный движок другим авторам или закрыть; только по просьбе пользователя |
| `looma_deploy` | `directory`, `app`, `promote` | собрать релиз и поставить на staging, с `promote=true` сразу и на prod; ждёт сборки минуты, при отказе возвращает причину и хвост лога |
| `looma_releases` | `app` | релизы сервиса: номер, статус, ошибка |
| `looma_logs` | `app`, `process`, `environment`, `tail` | лог процесса: у работающего с узла, у упавшего сохранённый хвост |
| `looma_check` | `app`, `environment`, `path`, `public` | запрос к веб-процессу сервиса: код, время, начало ответа. По умолчанию через API владельца, доходит и при `access: clients`; с `public=true` по публичному адресу, как гость. Спящий веб-процесс просыпается от запроса, до двух минут |
| `looma_promote` | `app`, `release` | перевести на prod релиз со staging или названный |
| `looma_rollback` | `app`, `environment` | вернуть окружение на прошлый релиз, по умолчанию prod |
| `looma_stop` | `app`, `environment` | остановить окружение, без `environment` оба |
| `looma_restart` | `app`, `process`, `environment` | перезапустить процесс, он подхватит новые секреты; задачу cron запустить сейчас |
| `looma_secrets_list` | `app` | имена секретов, без значений |
| `looma_secrets_set` | `app`, `values` | поставить секреты `{ИМЯ: значение}`, `null` убирает секрет |
| `looma_stats` | `app`, `days` | статистика сервиса за `days` дней: запросы по каналам, клиенты, события `charge`, вызовы моделей и себестоимость по прайсу |
| `looma_publish` | `app` | отправить на модерацию карточку с ценами из `looma.yaml` релиза на prod; только по просьбе пользователя, карточку увидят все |
| `looma_listing` | `app` | состояние карточки: в каталоге ли, что на модерации, причина отказа |
| `looma_unpublish` | `app` | снять карточку с каталога; вернуть её можно через `looma_publish` и модерацию. Только по просьбе пользователя |
| `looma_destroy` | `app`, `confirm` | удалить сервис со всеми данными. Необратимо: только по прямой просьбе пользователя, в `confirm` ещё раз имя сервиса |

Что такое свои модели и как устроена их цена: [Свои модели](own-models.md).

Пока идёт сборка, `looma_deploy` присылает ход работы уведомлениями о прогрессе, если
агент их принимает.

## Инструменты сервисов для клиентов

Инструменты сервисов каталога для клиентов устроены иначе: их описывает автор в
`channels.mcp` манифеста, площадка собирает из них MCP-сервер по адресу
`https://api.loomafloat.ru/mcp/<имя>`, а клиенты подключаются к нему со своим ключом.
Подробно: [Каналы](https://docs.loomafloat.ru/authors/channels/) и
[Подключение к ИИ-агентам](https://docs.loomafloat.ru/clients/agents/).
