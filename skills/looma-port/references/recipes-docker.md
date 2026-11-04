# Dockerfile, docker-compose и другие языки

## Свой Dockerfile

Укажите в `looma.yaml` `runtime: { kind: docker, dockerfile: Dockerfile }`. Так
собираются Go, Rust, Java, Ruby, PHP и всё, что площадка не собирает сама, а также
Python и Node.js с особой сборкой.

Образ собирает Kaniko без привилегий: 1 ядро, 1 ГБ памяти, до 30 минут, образ под
amd64. Интернет при сборке доступен через прокси (переменные заданы), поэтому `pip`,
`npm`, `apt` и `go mod download` работают.

Требования к образу:
- веб-процесс слушает `0.0.0.0:$PORT`;
- команда запуска задаётся в `processes.<имя>.cmd` или в `CMD` образа, если `cmd` не задан;
- `cmd` выполняется через `/bin/sh -c`, поэтому в образе нужен `sh`;
- у `scratch` и distroless `sh` нет: возьмите `alpine` или `debian-slim` либо задайте
  `CMD` образа и не пишите `cmd`;
- без `privileged`, монтирования и Docker внутри;
- ничего важного не пишите на диск, он временный ([Данные](data.md));
- рабочая папка берётся из `WORKDIR` образа.

Многоэтапная сборка (`FROM … AS build`, затем маленький образ) работает и полезна:
образ меньше, и сервис быстрее стартует после сна.

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

## docker-compose → один сервис Looma Float

Сервис на Looma Float собирается из одного образа и запускает из него несколько
процессов. Службы compose раскладываются так:

| Служба compose | На Looma Float |
|---|---|
| ваш код (`build: .`) | `processes.web` |
| воркер того же кода (`command: celery worker …`) | `processes.worker` |
| планировщик (`beat`, cron-контейнер) | процесс с `schedule:` |
| `postgres`, `pgvector`, `postgis` | `data.postgres` (`postgis` в списке расширений нет) |
| `minio` | `data.bucket` |
| `redis`, `rabbitmq`, `kafka` | замена в Postgres ([Данные](data.md#redis)) |
| `mongo`, `mysql`, `elasticsearch` | замена: [MongoDB](data.md#mongo), [MySQL](data.md#mysql), [поиск](data.md#search) |
| `nginx`, `traefik`, `caddy`, `certbot` | не нужны: домен, TLS и маршрутизацию даёт шлюз, статику отдаёт сам сервис |
| `ollama`, `vllm`, модели | модели сети Looma Float ([Модели](models.md)) |
| вторая служба своего кода (отдельный фронтенд, отдельный API) | соберите в один образ (фронтенд соберите и отдавайте из бэкенда) или сделайте отдельным сервисом, который обращается к первому по HTTPS |

Переменные из `environment:` compose перенесите так:
- адреса с именами служб (`db`, `redis`) больше не работают, вместо них подставьте
  `DATABASE_URL`, переменные бакета и другие переменные площадки
  ([Переменные окружения](env.md));
- секреты задайте командой `looma secrets set`;
- остальные значения сделайте значениями по умолчанию в коде или задайте в `cmd`.

## Фронтенд и бэкенд в одном образе

Частый случай: фронтенд на React или Vue и API в отдельных контейнерах. На Looma Float
соберите фронтенд при сборке образа и отдавайте его статикой из бэкенда:

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

Адрес API во фронтенде укажите относительным (`/api/...`): фронтенд и API работают на
одном домене.
