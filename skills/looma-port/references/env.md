# Переменные окружения

Площадка передаёт процессам сервиса доступы к данным, токен для вызова моделей и
сведения о релизе через переменные окружения. Свои значения задаются секретами:
`looma secrets set ИМЯ=значение`.

## Переменные процесса

| Переменная | Что |
|---|---|
| `PORT` | порт веб-процесса, тот, что в `processes.web.port` |
| `DATABASE_URL` | Postgres, если есть `data.postgres` |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`, `AWS_DEFAULT_REGION`, `LOOMA_BUCKET` | бакет, если есть `data.bucket` |
| `LOOMA_APP_TOKEN`, `LOOMA_API_URL` | токен и адрес API для вызовов моделей и учёта; адрес уже оканчивается на `/v1` |
| `LOOMA_APP`, `LOOMA_APP_ID`, `LOOMA_APP_URL` | имя, номер и адрес сервиса |
| `LOOMA_ENVIRONMENT` | `prod`, `staging` или `dev` в `looma dev` |
| `LOOMA_RELEASE`, `LOOMA_PROCESS`, `LOOMA_INSTANCE` | номер релиза, имя процесса, номер экземпляра |
| `HTTP_PROXY`, `HTTPS_PROXY` | прокси для выхода в интернет, те же имена и в нижнем регистре |
| имена из `secrets` | значения из `looma secrets set` |

В CLI, API и манифесте сервис называется app. Отсюда имена `LOOMA_APP*`, ключ
`--app` у команд `looma` и адреса `/v1/apps`.

## Занятые имена {#reserved}

Имена `PATH`, `HOME`, `PORT`, `DATABASE_URL`, `TMPDIR`, `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY` и все, что начинаются с `LOOMA_`, `AWS_`, `S3_`, заняты
площадкой: секрет с таким именем не задать.

## Пример

Строка подключения к базе, имя бакета и окружение:

```python
import os

db_url = os.environ["DATABASE_URL"]
bucket = os.environ["LOOMA_BUCKET"]
env = os.environ.get("LOOMA_ENVIRONMENT", "dev")
```
