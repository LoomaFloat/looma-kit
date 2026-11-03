# Модели: каталог Looma вместо своей видеокарты

В процессе приложения нет GPU. Модели считает сеть Looma, приложение вызывает их по
API. Что есть сейчас — `looma models` (или MCP `looma_models`): имя, тип, отвечает или
спит, контекст.

| Тип (`kind`) | Что делает | Адрес API |
|---|---|---|
| `text` | чат, генерация текста, инструменты | `POST /v1/chat/completions` (как у OpenAI, стрим — `stream: true`) |
| `image` | картинка по описанию, правка картинки | `POST /v1/images/generations`, `POST /v1/images/edits` |
| `video` | ролик по описанию (задание на минуты) | `POST /v1/videos`, `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content` |
| `embedding` | векторы текста | `POST /v1/embeddings` (как у OpenAI) |
| `rerank` | переупорядочить найденное по запросу | `POST /v1/rerank` (`query`, `documents`, `top_n`, как у Jina и Cohere) |

Адрес — `$LOOMA_API_URL`, ключ — `$LOOMA_APP_TOKEN` в `Authorization: Bearer`. Оба
платформа кладёт в окружение процесса. Каждую модель объявить в `gpu:` манифеста
(`<шаг>: { model: <имя> }`), иначе вызов ответит 403.

## Как подключить

**Код уже ходит в OpenAI (или совместимый API)** — поменять адрес, ключ и имя модели:

```python
from openai import OpenAI
client = OpenAI(base_url=os.environ["LOOMA_API_URL"] + "/v1",
                api_key=os.environ["LOOMA_APP_TOKEN"])
reply = client.chat.completions.create(model="<имя из looma models>", messages=[...])
```

```js
import OpenAI from "openai";
const client = new OpenAI({ baseURL: process.env.LOOMA_API_URL + "/v1",
                            apiKey: process.env.LOOMA_APP_TOKEN });
```

Чтобы проект работал и вне площадки — брать адрес, ключ и модель из своих переменных
с прежними значениями по умолчанию (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `MODEL`), а на
Looma задать их из `LOOMA_*` при старте. Node.js и прокси — [recipes-node.md](recipes-node.md).

**Python без OpenAI SDK** — SDK `loomafloat` (без зависимостей; `looma deploy` положит
его копию в проект сам, если код его импортирует):

```python
from loomafloat import Looma, client_of

looma = Looma()                                   # LOOMA_APP_TOKEN и LOOMA_API_URL — из окружения
text = looma.chat("<текстовая модель>", "Привет!")
for piece in looma.chat_stream("<текстовая модель>", messages): ...
png = looma.image("<модель картинок>", "рыжий кот", size="1024x1024")[0]
edited = looma.image("<модель картинок>", "надень шляпу", images=[png])[0]
mp4 = looma.video("<модель видео>", "A red fox in the snow", wait=True).content()
```

**Клиенты маркетплейса.** Если в манифесте `access: clients`, вызовы стоит записывать на
клиента: `looma.for_client(client_of(request.headers)).chat(...)` (без SDK — заголовок
`X-Looma-Client` из входящего запроса в запрос к модели), а готовый результат отмечать
событием: `looma.charge("answer", client)` (имя — в `events:` манифеста).

## Что чем заменить

| В проекте | Замена |
|---|---|
| Ollama, vLLM, llama.cpp, LocalAI, TGI | текстовая модель каталога через OpenAI-совместимый API; свой сервер модели и его сервис в docker-compose — убрать |
| `transformers` для генерации текста | текстовая модель каталога |
| `diffusers`, Stable Diffusion WebUI, ComfyUI | модель картинок каталога (генерация, правка по картинке) |
| `sentence-transformers`, `FlagEmbedding`, `fastembed` | `POST /v1/embeddings` моделью типа `embedding`; длина вектора — в ответе `looma models` (`dimensions`) |
| cross-encoder для ре-ранка | `POST /v1/rerank` моделью типа `rerank` |
| Whisper, TTS, распознавание изображений своими моделями | только если в `looma models` есть модель нужного типа; иначе — внешний API по HTTPS (ключ — секретом) или это блокер |
| OpenAI, Anthropic и другие внешние API | можно оставить: HTTPS наружу открыт, ключ — `looma secrets set`; или перевести на каталог |
| Веса моделей в репозитории | убрать из релиза (`.loomaignore`) |

После замены убрать из зависимостей `torch`, `tensorflow`, `transformers`,
`diffusers` и подобное, если их больше ничего не использует: сборке даётся 1 ГБ
памяти и 30 минут, а образ с ними весит гигабайты.

## Поведение моделей

- **Модель спит** (`looma models` пишет «спит») — первый вызов поднимает её: запрос
  ждёт до 4 минут; не дождался — 503 с `Retry-After: 30`. В коде — таймаут клиента не
  меньше 5 минут и повтор при 503 через `Retry-After`.
- **429** — модель занята; повторить позже (SDK `loomafloat` повторяет сам до трёх раз).
- **Видео** — задание на минуты: сохранить номер задания, отдать пользователю ответ
  сразу, забрать результат опросом `GET /v1/videos/{id}` (статусы `queued`,
  `in_progress`, `completed`, `failed`, `cancelled`), потом `content`. Процесс может
  уснуть — номер задания хранить в базе, а не в памяти.
- **Контекст текстовой модели** — в `looma models`; длинные документы резать на куски.
