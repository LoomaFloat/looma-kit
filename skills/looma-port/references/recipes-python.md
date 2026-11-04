# Python: рецепты переноса

Укажите в `looma.yaml` `runtime: { kind: python, python: "3.12" }`. Площадка ставит
зависимости из `requirements.txt` (или `pip install .` по `pyproject.toml`) и кладёт код
в `/app`. Если вы используете Poetry, Pipenv или uv, выгрузите зависимости в
`requirements.txt` (`uv export --no-hashes -o requirements.txt`,
`poetry export -f requirements.txt`) или соберите образ своим Dockerfile
([Dockerfile и docker-compose](recipes-docker.md)).

## Запуск веба

Веб-процесс должен слушать `0.0.0.0` на порту `$PORT`. Команду запуска укажите в
`processes.web.cmd`: она выполняется через shell, поэтому `$PORT` раскрывается. Сервер
разработки (`app.run()`, `runserver`) замените на боевой.

| Фреймворк | `cmd` | Зависимость |
|---|---|---|
| Flask | `gunicorn app:app -b 0.0.0.0:$PORT --workers 2 --timeout 300` | `gunicorn` |
| FastAPI, Starlette | `uvicorn main:app --host 0.0.0.0 --port $PORT` | `uvicorn` |
| Django | `python manage.py migrate --noinput && gunicorn proj.wsgi -b 0.0.0.0:$PORT` | `gunicorn` |
| aiohttp | `python -m aiohttp.web -H 0.0.0.0 -P $PORT app:init` или `web.run_app(app, host="0.0.0.0", port=int(os.environ["PORT"]))` | не нужна |
| свой `if __name__ == "__main__"` | `python app.py` и в коде `port=int(os.environ.get("PORT", 8080))`, `host="0.0.0.0"` | не нужна |

Модели могут отвечать долго. Таймаут воркера gunicorn (`--timeout`) должен быть больше
времени ответа модели. Для потоковых ответов используйте `uvicorn` или
`gunicorn -k uvicorn.workers.UvicornWorker`.

## Django

- В `ALLOWED_HOSTS` добавьте хост из `LOOMA_APP_URL` (или `.apps.loomafloat.ru`).
- Если в сервисе есть формы, задайте `CSRF_TRUSTED_ORIGINS = [os.environ["LOOMA_APP_URL"]]`.
- Базу подключите через `dj-database-url` из `DATABASE_URL` ([Данные](data.md#sqlite)).
- Статику отдавайте через `whitenoise`: задайте `STATIC_ROOT` и выполните `collectstatic`
  в `cmd` перед запуском или при сборке.
- Медиа храните в бакете через `django-storages` ([Данные](data.md#files)).
- `SECRET_KEY` и `DEBUG` читайте из переменных окружения. `SECRET_KEY` задайте секретом,
  `DEBUG` по умолчанию держите `False`.
- HTTPS обеспечивает шлюз, поэтому задайте
  `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`.

## Фоновые задачи

- Celery или RQ с Redis замените очередью в Postgres (`procrastinate`) и процессом
  `worker` ([Данные](data.md#redis)). Если задачи короткие и редкие, выполняйте их в
  потоке веб-процесса, а состояние храните в базе.
- `APScheduler` или `schedule` внутри веба замените процессом по расписанию:
  `nightly: { schedule: "0 3 * * *", cmd: "python -m app.nightly" }` (время в UTC). Веб
  с `min: 0` засыпает, и планировщик внутри него не сработает.

## Модели

В OpenAI SDK укажите адрес `$LOOMA_API_URL` (он уже оканчивается на `/v1`) и ключ `$LOOMA_APP_TOKEN` или
используйте SDK `loomafloat` ([Модели](models.md)). `requests`, `httpx`, `urllib` и
`boto3` берут прокси из окружения сами. `aiohttp` этого не делает: создавайте сессию
как `aiohttp.ClientSession(trust_env=True)`.

## Gradio, Streamlit, Chainlit

Streamlit работает через WebSocket, а шлюз WebSocket не пропускает. Интерфейс придётся
переписать на обычный веб: FastAPI или Flask и простая страница, поток текста через SSE.
Chainlit тоже работает через WebSocket, путь для него тот же.

Gradio проверьте на staging: выполните `looma check`, затем откройте адрес staging и
пройдите интерфейс. Если интерфейс не отвечает, перепишите его так же, как Streamlit.

## Типичный набор правок

1. В `requirements.txt` добавьте `gunicorn` или `uvicorn`, драйвер Postgres и `boto3`.
   Уберите `torch`, `transformers` и прочее, если модели переехали в сеть Looma Float.
2. Слушайте `0.0.0.0` на порту из `PORT`.
3. Базу подключите по `DATABASE_URL`, файлы храните в бакете.
4. Модели вызывайте по API сети Looma Float.
5. Добавьте `looma.yaml` и `.loomaignore`.
