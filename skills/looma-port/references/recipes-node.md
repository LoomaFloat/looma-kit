# Node.js: рецепты переноса

Укажите в `looma.yaml` `runtime: { kind: node, node: "22" }`. Площадка ставит
зависимости (`npm ci`, если есть `package-lock.json`, иначе `npm install`), выполняет
`npm run build --if-present` и задаёт `NODE_ENV=production`. Если вы используете pnpm
или yarn, сгенерируйте `package-lock.json` (`npm install --package-lock-only`) или
соберите образ своим Dockerfile ([Dockerfile и docker-compose](recipes-docker.md)).

## Запуск веба

Веб-процесс должен слушать `0.0.0.0` на порту из `process.env.PORT`:

```js
const port = Number(process.env.PORT || 3000);
app.listen(port, "0.0.0.0");
```

| Фреймворк | `cmd` |
|---|---|
| Express, Fastify, Koa, Hono | `node server.js` (или `npm start`) |
| Next.js | `npx next start -H 0.0.0.0 -p $PORT` |
| Nuxt | `HOST=0.0.0.0 node .output/server/index.mjs` (Nitro берёт порт из `PORT`) |
| NestJS | `node dist/main.js` и `await app.listen(process.env.PORT, "0.0.0.0")` |
| SvelteKit (adapter-node) | `node build` (слушает `PORT`, `HOST=0.0.0.0`) |
| Remix, Astro (node) | команда `start` из `package.json`, с `PORT` и `0.0.0.0` |

`devDependencies` при сборке ставятся (`npm ci` без `--omit=dev`), поэтому сборка
фронтенда работает. Сборка ограничена 1 ГБ памяти и 30 минутами, и большой фронтенд
может не уложиться. Тогда задайте `NODE_OPTIONS=--max-old-space-size=900` в скрипте
сборки или положите готовую сборку в архив релиза.

## Прокси: выход в интернет из Node.js

Из песочницы наружу можно выйти только через прокси (`HTTP_PROXY`, `HTTPS_PROXY`).
Встроенный `fetch` Node.js и многие клиенты на нём (в том числе OpenAI SDK) по
умолчанию переменные прокси не читают. Без настройки запросы наружу и к
`LOOMA_API_URL` не пройдут. Выберите один из вариантов.

1. Включите прокси для всего процесса через `undici` в точке входа:

   ```js
   import { setGlobalDispatcher, EnvHttpProxyAgent } from "undici";
   setGlobalDispatcher(new EnvHttpProxyAgent());
   ```

   Добавьте пакет `undici` в зависимости. Настройка действует на глобальный `fetch`.
2. Если ваша версия Node.js поддерживает переменную `NODE_USE_ENV_PROXY=1`, задайте её
   в `cmd`: `NODE_USE_ENV_PROXY=1 node server.js`. Проверьте на staging, что вызовы
   проходят.
3. `axios` читает `HTTPS_PROXY` сам. Для `node-fetch` и `got` подключите агент
   `https-proxy-agent`.

Проверьте результат командой `looma logs --env staging`. Прокси пишет в лог процесса
строку на каждый адрес, к которому обращался сервис. Ошибки соединения видны там же.

## Модели

```js
import OpenAI from "openai";
const looma = new OpenAI({ baseURL: process.env.LOOMA_API_URL,
                           apiKey: process.env.LOOMA_APP_TOKEN, timeout: 300_000 });
```

Имена моделей возьмите из `looma models` и каждую укажите в разделе `gpu:` манифеста
([Модели](models.md)). Для Vercel AI SDK используйте `createOpenAI({ baseURL, apiKey })`
из `@ai-sdk/openai` с теми же значениями.

## Данные

Для `pg` задайте `connectionString: process.env.DATABASE_URL`. Для Prisma укажите
`provider = "postgresql"` и выполняйте `prisma migrate deploy` в `cmd` перед запуском.
Файлы храните в бакете через `@aws-sdk/client-s3` с `forcePathStyle: true`
([Данные](data.md#files)).

Сессии в памяти процесса теряются, когда сервис засыпает. Храните их в Postgres
(`connect-pg-simple`) или в подписанных cookie.

## WebSocket, socket.io

Шлюз WebSocket не пропускает. Без сокета socket.io переходит на HTTP long-polling:
задайте `transports: ["polling"]` на клиенте и на сервере. Для уведомлений этого
хватает, но проверьте работу на staging.

Потоковый ответ модели отдавайте через SSE (`text/event-stream`).
