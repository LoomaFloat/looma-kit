# Dockerfile, docker-compose и другие языки

## Свой Dockerfile

`runtime: { kind: docker, dockerfile: Dockerfile }` — для Go, Rust, Java, Ruby, PHP и
всего, что платформа не собирает сама, а также для Python и Node.js с особой сборкой.

Сборка — Kaniko без привилегий: 1 ядро, 1 ГБ памяти, до 30 минут, образ под amd64.
Интернет при сборке — через прокси (переменные заданы), так что `pip`, `npm`, `apt`,
`go mod download` работают.

Требования к образу:
- процесс веба слушает `0.0.0.0:$PORT`;
- команда запуска — в `processes.<имя>.cmd` (или `CMD` образа, если `cmd` не задан);
  `cmd` выполняется через `/bin/sh -c` — в образе нужен `sh` (у `scratch` и
  distroless его нет: брать `alpine`, `debian-slim` или задавать `CMD` и не писать `cmd`);
- без `privileged`, монтирования и Docker внутри;
- ничего важного не писать на диск: он временный ([data.md](data.md));
- рабочая папка — `WORKDIR` образа.

Многоэтапная сборка (`FROM … AS build`, затем маленький образ) — нормально и полезно:
меньше образ, быстрее старт после сна.

Пример для Go:

```dockerfile
FROM golang:1.23 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /app/server ./cmd/server

FROM alpine:3.20
COPY --from=build /app/server /app/server
WORKDIR /app
CMD ["/app/server"]
```

```yaml
runtime: { kind: docker, dockerfile: Dockerfile }
processes:
  web: { cmd: "/app/server", port: 8080 }
```

## docker-compose → одно приложение Looma

Приложение Looma — один образ и несколько процессов из него. Сервисы compose
раскладываются так:

| Сервис compose | На Looma |
|---|---|
| приложение (`build: .`) | `processes.web` |
| воркер того же кода (`command: celery worker …`) | `processes.worker` |
| планировщик (`beat`, cron-контейнер) | процесс с `schedule:` |
| `postgres`, `pgvector`, `postgis` | `data.postgres` (`postgis` в списке расширений нет) |
| `minio` | `data.bucket` |
| `redis`, `rabbitmq`, `kafka` | замена в Postgres ([data.md](data.md#redis)) |
| `mongo`, `mysql`, `elasticsearch` | замена ([data.md](data.md)) |
| `nginx`, `traefik`, `caddy`, `certbot` | не нужны: домен, TLS и маршрутизацию даёт шлюз; статику отдаёт приложение |
| `ollama`, `vllm`, модели | каталог Looma ([models.md](models.md)) |
| второй сервис своего кода (отдельный фронтенд, отдельный API) | склеить в один образ (фронтенд собрать и отдавать из бэкенда) или сделать отдельным приложением Looma, которое ходит к первому по HTTPS |

Переменные из `environment:` compose: имена служб (`db`, `redis`) в адресах больше не
работают — подставить `DATABASE_URL`, бакет и т. д.; секреты — `looma secrets set`;
остальное — значениями по умолчанию в коде или в `cmd`.

## Фронтенд и бэкенд в одном образе

Частый случай: React/Vue-фронтенд и API отдельными контейнерами. На Looma — собрать
фронтенд при сборке и отдавать его статикой из бэкенда:

```dockerfile
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/ .
COPY --from=web /web/dist ./static
```

Адрес API во фронтенде — относительный (`/api/...`), раз они на одном домене.
