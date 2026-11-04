# Данные: Postgres и бакет вместо локального диска и других баз

На площадке два хранилища: Postgres (`data.postgres`) и S3-бакет (`data.bucket`). Диск
процесса временный: он стирается при перезапуске, сне и новом релизе, а у нескольких
экземпляров он у каждого свой. Всё, что должно пережить перезапуск, храните в базе или
бакете.

## Подключите хранилища

```yaml
data:
  postgres: { extensions: [vector] }
  bucket: {}
```

| Хранилище | Что получает процесс |
|---|---|
| Postgres | строку подключения в `DATABASE_URL` |
| Бакет | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION` и имя бакета в `LOOMA_BUCKET` |

Расширения Postgres выбираются из списка: `vector`, `pg_trgm`, `uuid-ossp`, `citext`,
`hstore`, `pgcrypto`, `btree_gin`, `btree_gist`, `unaccent`, `fuzzystrmatch`.

Таблицы и миграции создаёт сам сервис: при старте веба или отдельной командой перед
ним, например `cmd: "python manage.py migrate && gunicorn …"`.

## Чем заменить то, чего нет

| Было | Замена |
|---|---|
| SQLite, MySQL, MariaDB | Postgres: [SQLite](#sqlite), [MySQL](#mysql) |
| MongoDB | Postgres с `jsonb`: [MongoDB](#mongo) |
| Redis | память процесса или таблица в Postgres: [Redis](#redis) |
| Очередь задач, брокеры | таблица задач в Postgres и процесс `worker`: [Брокеры](#broker) |
| Elasticsearch, Meilisearch, Qdrant, Chroma | поиск и pgvector в Postgres: [Поиск](#search) |
| Файлы на диске | бакет: [Файлы](#files) |

## SQLite → Postgres {#sqlite}

1. Добавьте в манифест `data: { postgres: {} }`.
2. Берите строку подключения из `DATABASE_URL` (формат `postgresql://…`).
3. Драйвер: для Python `psycopg[binary]` или `psycopg2-binary` (асинхронный
   `asyncpg`), для Node.js `pg`.
4. Проверьте SQL на особенности SQLite: `AUTOINCREMENT` → `GENERATED ... AS IDENTITY`
   или `SERIAL`, `INSERT OR REPLACE` → `INSERT ... ON CONFLICT ... DO UPDATE`,
   `datetime('now')` → `now()`, булевы `0/1` → `true/false`. В Postgres `LIKE`
   различает регистр, без учёта регистра нужен `ILIKE`.
5. Схема: создавайте таблицы при старте (`CREATE TABLE IF NOT EXISTS`) или миграциями
   перед запуском веба.

По фреймворкам:

| Что | Правка |
|---|---|
| SQLAlchemy | `create_engine(os.environ["DATABASE_URL"])`; схему `postgres://` замените на `postgresql+psycopg://` |
| Django | `dj-database-url`: `DATABASES = {"default": dj_database_url.config(default="sqlite:///db.sqlite3")}` |
| Peewee | `PostgresqlDatabase` через `playhouse.db_url.connect(os.environ["DATABASE_URL"])` |
| Prisma | `provider = "postgresql"`, `url = env("DATABASE_URL")`, `prisma migrate deploy` перед запуском |
| Knex, Sequelize, TypeORM, Drizzle | клиент `pg`, `connectionString: process.env.DATABASE_URL` |

Существующие данные переносятся отдельно. Снаружи к базе площадки не подключиться,
поэтому данные загружает сам сервис: одноразовым шагом при старте (с отметкой в базе,
чтобы шаг не повторялся) или через свой API.

## MySQL/MariaDB → Postgres {#mysql}

Шаги те же, что для SQLite. Отличия диалекта: кавычки идентификаторов `` ` `` → `"`,
`AUTO_INCREMENT` → `GENERATED ... AS IDENTITY`, `ON DUPLICATE KEY UPDATE` →
`ON CONFLICT ... DO UPDATE`, `LIMIT a, b` → `LIMIT b OFFSET a`, `IFNULL` → `COALESCE`,
`TINYINT(1)` → `boolean`. ORM обычно справляется сменой драйвера и строки
подключения.

## MongoDB → Postgres {#mongo}

Переделка большая. MongoDB на площадке нет, а внешний (Atlas) работает на порту
27017, который наружу закрыт. Обычный путь: коллекция становится таблицей
`(id uuid primary key, data jsonb, …)`, частые поля запросов выносятся в отдельные
колонки с индексами, остальное лежит в `jsonb` (операторы `->>`, `@>`, индекс GIN).

## Redis → Postgres или память процесса {#redis}

Redis на площадке нет, а внешний работает на порту 6379, который закрыт.

| Для чего был Redis | Замена |
|---|---|
| Кэш | память процесса (`functools.lru_cache`, `cachetools`, `lru-cache`) или таблица в Postgres с полем `expires_at` |
| Сессии | подписанные cookie или таблица в Postgres |
| Очередь задач (Celery, RQ, Bull) | таблица задач в Postgres и процесс `worker`, который берёт задачи через `SELECT … FOR UPDATE SKIP LOCKED`; готовые решения: для Python `procrastinate`, для Node.js `pg-boss` и `graphile-worker` |
| Pub/sub | `LISTEN`/`NOTIFY` Postgres |
| Счётчики, лимиты | `UPDATE … SET n = n + 1 … RETURNING n` в Postgres |

Celery с брокером Redis проще всего заменить на `procrastinate` (Python), сохранив сами
функции задач. Расписание Celery beat переходит в процесс по расписанию (`schedule` в
манифесте).

## Брокеры (RabbitMQ, Kafka, NATS) {#broker}

На площадке их нет, порты закрыты. Очередь делается таблицей в Postgres и процессом
`worker`, как в разделе [Redis](#redis). Если брокер был шиной между несколькими
частями проекта, это повод собрать их в один сервис с несколькими процессами.

## Поисковые движки и векторные базы {#search}

| Было | Замена |
|---|---|
| Elasticsearch, OpenSearch, Meilisearch, Typesense | полнотекстовый поиск Postgres: `tsvector`, `to_tsquery`, индекс GIN; для нечёткого поиска `pg_trgm` |
| Qdrant, Chroma, Pinecone, Milvus, Weaviate | pgvector через `data.postgres: { extensions: [vector] }`; векторы считает модель эмбеддингов сети (`looma models`, тип `embedding`) |

## Файлы на диске → бакет {#files}

1. Добавьте в манифест `data: { bucket: {} }`.
2. Создайте клиент S3 с адресацией по пути (path-style). Доступы и адрес клиент берёт
   из окружения сам.

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
- загрузки пользователей, сгенерированные файлы и результаты храните в бакете;
- отдавайте их через сервис (прочитать из бакета и вернуть ответом) или временной
  ссылкой (presigned URL);
- временные файлы на время запроса можно класть в `/tmp`;
- `UPLOAD_FOLDER` и `MEDIA_ROOT` Django переведите на хранилище S3: `django-storages`
  с `AWS_S3_ENDPOINT_URL`, `AWS_S3_ADDRESSING_STYLE = "path"` и
  `AWS_STORAGE_BUCKET_NAME` из `LOOMA_BUCKET`.

Чтобы проект работал и вне площадки, пишите в локальную папку, как раньше, если
`LOOMA_BUCKET` не задан.

## Где видно

Вкладка «Данные» сервиса в кабинете автора показывает базу и бакет. Пароли и ключи
там не показываются: процессы получают их переменными окружения.
