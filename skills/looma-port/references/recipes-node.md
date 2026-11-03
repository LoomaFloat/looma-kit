# Node.js: рецепты переноса

`runtime: { kind: node, node: "22" }`. Платформа ставит зависимости (`npm ci`, если
есть `package-lock.json`, иначе `npm install`), выполняет `npm run build --if-present`
и задаёт `NODE_ENV=production`. pnpm и yarn: сгенерировать `package-lock.json`
(`npm install --package-lock-only`) или собрать своим Dockerfile
([recipes-docker.md](recipes-docker.md)).

## Запуск веба

Слушать `0.0.0.0` на `process.env.PORT`:

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

`devDependencies` при сборке ставятся (`npm ci` без `--omit=dev`), так что сборка
фронтенда работает. Сборка ограничена 1 ГБ памяти и 30 минутами: большой фронтенд
может не уложиться — тогда `NODE_OPTIONS=--max-old-space-size=900` в скрипте сборки
или готовая сборка в архиве релиза.

## Прокси: выход в интернет из Node.js

Из песочницы наружу можно только через прокси (`HTTP_PROXY`, `HTTPS_PROXY`).
Встроенный `fetch` Node.js и многие клиенты на нём (в том числе OpenAI SDK) по
умолчанию переменные прокси не читают — запросы наружу и к `LOOMA_API_URL` не
пройдут. Варианты:

1. Включить прокси для всего процесса через `undici` в точке входа:

   ```js
   import { setGlobalDispatcher, EnvHttpProxyAgent } from "undici";
   setGlobalDispatcher(new EnvHttpProxyAgent());
   ```

   (пакет `undici` в зависимости; влияет на глобальный `fetch`).
2. Node.js с поддержкой переменной `NODE_USE_ENV_PROXY=1` — задать её в `cmd`:
   `NODE_USE_ENV_PROXY=1 node server.js`. Проверить на staging, что вызовы проходят.
3. `axios` читает `HTTPS_PROXY` сам; для `node-fetch`, `got` — агент
   `https-proxy-agent`.

Как проверить: `looma logs --env staging` — прокси пишет в лог процесса строку на
каждый адрес, куда ходило приложение; ошибки соединения видны там же.

## Модели

```js
import OpenAI from "openai";
const looma = new OpenAI({ baseURL: process.env.LOOMA_API_URL + "/v1",
                           apiKey: process.env.LOOMA_APP_TOKEN, timeout: 300_000 });
```

Имена моделей — из `looma models`, каждую — в `gpu:` манифеста ([models.md](models.md)).
Vercel AI SDK: `createOpenAI({ baseURL, apiKey })` из `@ai-sdk/openai` с теми же
значениями.

## Данные

`pg` с `connectionString: process.env.DATABASE_URL`; Prisma — `provider = "postgresql"`
и `prisma migrate deploy` в `cmd` перед запуском. Файлы — `@aws-sdk/client-s3` с
`forcePathStyle: true` ([data.md](data.md#files)). Сессии в памяти процесса теряются
при сне — хранить в Postgres (`connect-pg-simple`) или подписанных cookie.

## WebSocket, socket.io

Шлюз WebSocket не пропускает. socket.io без сокета откатывается на HTTP long-polling:
`transports: ["polling"]` на клиенте и сервере — вариант для уведомлений, проверить на
staging.
Стрим ответа модели — SSE (`text/event-stream`).
