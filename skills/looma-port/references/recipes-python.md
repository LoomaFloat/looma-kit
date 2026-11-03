# Python: рецепты переноса

`runtime: { kind: python, python: "3.12" }`. Платформа ставит зависимости из
`requirements.txt` (или `pip install .` по `pyproject.toml`) и кладёт код в `/app`.
Poetry, Pipenv, uv: выгрузить зависимости в `requirements.txt`
(`uv export --no-hashes -o requirements.txt`, `poetry export -f requirements.txt`) или
собрать своим Dockerfile ([recipes-docker.md](recipes-docker.md)).

## Запуск веба

Слушать `0.0.0.0` на `$PORT`. Команда — в `processes.web.cmd` (выполняется через
shell, `$PORT` раскрывается). Сервер разработки (`app.run()`, `runserver`) заменить на
боевой.

| Фреймворк | `cmd` | Зависимость |
|---|---|---|
| Flask | `gunicorn app:app -b 0.0.0.0:$PORT --workers 2 --timeout 300` | `gunicorn` |
| FastAPI, Starlette | `uvicorn main:app --host 0.0.0.0 --port $PORT` | `uvicorn` |
| Django | `python manage.py migrate --noinput && gunicorn proj.wsgi -b 0.0.0.0:$PORT` | `gunicorn` |
| aiohttp | `python -m aiohttp.web -H 0.0.0.0 -P $PORT app:init` или `web.run_app(app, host="0.0.0.0", port=int(os.environ["PORT"]))` | — |
| свой `if __name__ == "__main__"` | `python app.py` и в коде `port=int(os.environ.get("PORT", 8080))`, `host="0.0.0.0"` | — |

Долгие ответы моделей: таймаут воркера gunicorn (`--timeout`) — больше времени ответа
модели; для стрима — `uvicorn` или `gunicorn -k uvicorn.workers.UvicornWorker`.

## Django

- `ALLOWED_HOSTS`: добавить хост из `LOOMA_APP_URL` (или `.apps.loomafloat.ru`);
  `CSRF_TRUSTED_ORIGINS = [os.environ["LOOMA_APP_URL"]]`, если есть формы.
- База — `dj-database-url` из `DATABASE_URL` ([data.md](data.md)).
- Статика: `whitenoise` (`STATIC_ROOT`, `collectstatic` в `cmd` перед запуском или при
  сборке), медиа — `django-storages` в бакет.
- `SECRET_KEY`, `DEBUG=False` — секретами и переменными.
- HTTPS делает шлюз: `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`.

## Фоновые задачи

- Celery/RQ с Redis → очередь в Postgres (`procrastinate`) и процесс `worker`
  ([data.md](data.md#redis)). Если задачи короткие и редкие — выполнять в потоке
  веб-процесса и хранить состояние в базе.
- `APScheduler`, `schedule` в процессе веба → процесс по расписанию:
  `nightly: { schedule: "0 3 * * *", cmd: "python -m app.nightly" }` (UTC). Веб с
  `min: 0` засыпает — внутрипроцессный планировщик в нём не сработает.

## Модели

OpenAI SDK → адрес `$LOOMA_API_URL/v1` и ключ `$LOOMA_APP_TOKEN`; либо SDK
`loomafloat` ([models.md](models.md)). `requests`, `httpx`, `urllib`, `boto3` берут
прокси из окружения сами. `aiohttp` — нет: `aiohttp.ClientSession(trust_env=True)`.

## Gradio, Streamlit, Chainlit

Streamlit держится на WebSocket — шлюз его не пропускает: интерфейс переписать на
обычный веб (FastAPI или Flask и простая страница; стрим текста — SSE). Gradio —
проверить на staging (`looma check`, затем открыть в браузере): если интерфейс не
отвечает — тот же путь. Chainlit — на WebSocket, как Streamlit.

## Типичный набор правок

1. `requirements.txt`: добавить `gunicorn` или `uvicorn`, драйвер Postgres, `boto3`;
   убрать `torch`, `transformers` и прочее, если модели переехали в каталог.
2. Порт и адрес — из `PORT`.
3. База — `DATABASE_URL`; файлы — бакет.
4. Модели — каталог Looma.
5. `looma.yaml`, `.loomaignore`.
