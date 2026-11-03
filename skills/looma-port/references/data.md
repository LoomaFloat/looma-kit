# Данные: Postgres и бакет вместо локального диска и чужих баз

На площадке два хранилища: Postgres (`data.postgres`) и S3-бакет (`data.bucket`). Диск
процесса временный: стирается при перезапуске, сне и релизе, а у нескольких
экземпляров он у каждого свой. Всё, что должно пережить перезапуск, — в базу или бакет.

## SQLite → Postgres {#sqlite}

1. В манифест: `data: { postgres: {} }`.
2. Строка подключения — из `DATABASE_URL` (формат `postgresql://…`).
3. Драйвер: Python — `psycopg[binary]` или `psycopg2-binary` (async — `asyncpg`);
   Node.js — `pg`.
4. Проверить SQL на особенности SQLite: `AUTOINCREMENT` → `GENERATED ... AS IDENTITY`
   или `SERIAL`, `INSERT OR REPLACE` → `INSERT ... ON CONFLICT ... DO UPDATE`,
   `datetime('now')` → `now()`, булевы `0/1` → `true/false`, регистр в `LIKE` (в
   Postgres он важен — `ILIKE`).
5. Схема: создать таблицы при старте (`CREATE TABLE IF NOT EXISTS`) или миграциями
   перед запуском веба.

По фреймворкам:

| Что | Правка |
|---|---|
| SQLAlchemy | `create_engine(os.environ["DATABASE_URL"])`; для `postgres://` заменить на `postgresql+psycopg://` |
| Django | `dj-database-url`: `DATABASES = {"default": dj_database_url.config(default="sqlite:///db.sqlite3")}` |
| Peewee | `PostgresqlDatabase` через `playhouse.db_url.connect(os.environ["DATABASE_URL"])` |
| Prisma | `provider = "postgresql"`, `url = env("DATABASE_URL")`, `prisma migrate deploy` перед запуском |
| Knex, Sequelize, TypeORM, Drizzle | клиент `pg`, `connectionString: process.env.DATABASE_URL` |

Существующие данные переносятся отдельно и только с согласия пользователя. Снаружи к
базе площадки не подключиться, поэтому данные загружает само приложение: одноразовым
шагом при старте (с отметкой в базе, чтобы не повторялся) или через свой API.

## MySQL/MariaDB → Postgres {#mysql}

Те же шаги, что для SQLite. Отличия диалекта: кавычки идентификаторов `` ` `` → `"`,
`AUTO_INCREMENT` → `GENERATED ... AS IDENTITY`, `ON DUPLICATE KEY UPDATE` →
`ON CONFLICT ... DO UPDATE`, `LIMIT a, b` → `LIMIT b OFFSET a`, `IFNULL` → `COALESCE`,
`TINYINT(1)` → `boolean`. ORM обычно справляется сменой драйвера и строки подключения.

## MongoDB → Postgres {#mongo}

Большая переделка: MongoDB на площадке нет, внешний (Atlas) — порт 27017, наружу
закрыт. Обычный путь: коллекция → таблица `(id uuid primary key, data jsonb, …)`,
частые поля запросов — отдельными колонками с индексами, остальное — в `jsonb`
(операторы `->>`, `@>`, индекс GIN). Оценить объём с пользователем до начала.

## Redis → Postgres или память процесса {#redis}

Redis на площадке нет, внешний — порт 6379, закрыт.

| Для чего был Redis | Замена |
|---|---|
| Кэш | в памяти процесса (`functools.lru_cache`, `cachetools`, `lru-cache`) или таблица в Postgres с полем `expires_at` |
| Сессии | подписанные cookie или таблица в Postgres |
| Очередь задач (Celery, RQ, Bull) | таблица задач в Postgres + процесс `worker`, который берёт задачи `SELECT … FOR UPDATE SKIP LOCKED`; для Python готовое — `procrastinate`, для Node.js — `pg-boss`, `graphile-worker` |
| Pub/sub | `LISTEN`/`NOTIFY` Postgres |
| Счётчики, лимиты | `UPDATE … SET n = n + 1 … RETURNING n` в Postgres |

Celery с брокером Redis: проще всего заменить на `procrastinate` (Python), сохранив
сами функции задач. Расписание Celery beat → процесс по расписанию (`schedule:` в
манифесте).

## Брокеры (RabbitMQ, Kafka, NATS) {#broker}

Нет на площадке, порты закрыты. Очередь — таблица в Postgres и `worker` (см. Redis).
Если брокер нужен как шина между несколькими сервисами — это повод склеить их в
одно приложение с несколькими процессами.

## Поисковые движки и векторные базы {#search}

| Было | Замена |
|---|---|
| Elasticsearch, OpenSearch, Meilisearch, Typesense | полнотекстовый поиск Postgres: `tsvector`, `to_tsquery`, индекс GIN; нечёткий — `pg_trgm` |
| Qdrant, Chroma, Pinecone, Milvus, Weaviate | `data.postgres: { extensions: [vector] }` и pgvector; векторы — моделью эмбеддингов из каталога (`looma models`, тип `embedding`) |

## Файлы на диске → бакет {#files}

1. В манифест: `data: { bucket: {} }`.
2. Клиент S3 с адресацией по пути. Доступы и адрес берутся из окружения сами.

Python (boto3):

```python
import os, boto3
from botocore.config import Config

s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
                  config=Config(s3={"addressing_style": "path"}))
BUCKET = os.environ["LOOMA_BUCKET"]

s3.put_object(Bucket=BUCKET, Key=f"uploads/{name}", Body=data, ContentType=mime)
body = s3.get_object(Bucket=BUCKET, Key=f"uploads/{name}")["Body"].read()
url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key},
                                ExpiresIn=3600)
```

Node.js (`@aws-sdk/client-s3`):

```js
import { S3Client, PutObjectCommand, GetObjectCommand } from "@aws-sdk/client-s3";

const s3 = new S3Client({ endpoint: process.env.AWS_ENDPOINT_URL_S3,
                          region: process.env.AWS_REGION, forcePathStyle: true });
await s3.send(new PutObjectCommand({ Bucket: process.env.LOOMA_BUCKET, Key: key, Body: data }));
```

Что куда:
- загрузки пользователей, сгенерированные файлы, результаты — в бакет;
- отдавать — через приложение (читать из бакета и отдавать ответом) или временной
  ссылкой (presigned URL);
- временные файлы на время запроса — в `/tmp`, это нормально;
- `UPLOAD_FOLDER`, `MEDIA_ROOT` Django — через хранилище S3 (`django-storages` с
  `AWS_S3_ENDPOINT_URL`, `AWS_S3_ADDRESSING_STYLE = "path"`, `AWS_STORAGE_BUCKET_NAME`
  из `LOOMA_BUCKET`).

Чтобы проект работал и вне площадки: если `LOOMA_BUCKET` не задан — писать в
локальную папку, как раньше.
