# Модели: сеть Looma Float вместо своей видеокарты

В процессе сервиса нет видеокарты. Модели считает сеть Looma Float, сервис вызывает
их по API. Какие модели есть сейчас, показывает `looma models` (или MCP-инструмент
`looma_models`): имя, тип, отвечает или спит, контекст.

## Объявите модели в манифесте

Каждую модель, которую вызывает сервис, объявите в разделе `gpu` манифеста:
`<шаг>: { model: <имя> }`. Имя шага выбираете вы, имя модели берётся из
`looma models` как есть.

```yaml
gpu:
  chat: { model: Qwen3.8-Flash-Next-NVFP4 }
  draw: { model: Qwen-Image-2.1 }
```

Токен сервиса пускает только к объявленным моделям. Вызов другой модели ответит `403`.

## Типы моделей

| Тип (`kind`) | Что делает | Адрес API |
|---|---|---|
| `text` | чат, генерация текста, инструменты; ответ можно получать потоком (`stream: true`) | `POST /v1/chat/completions`, как у OpenAI |
| `image` | изображение по описанию, правка изображения | `POST /v1/images/generations`, `POST /v1/images/edits` |
| `video` | ролик по описанию, задание на минуты | `POST /v1/videos`, `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content` |
| `embedding` | векторы текста | `POST /v1/embeddings`, как у OpenAI |
| `rerank` | переупорядочить найденное по запросу | `POST /v1/rerank` (`query`, `documents`, `top_n`), как у Jina и Cohere |

Адрес API площадка кладёт в окружение процесса как `LOOMA_API_URL`, токен как
`LOOMA_APP_TOKEN`. Адрес уже оканчивается на `/v1`: чат, например, отвечает на
`$LOOMA_API_URL/chat/completions`. Токен передаётся в `Authorization: Bearer`.

## Вызов из кода

**Код уже ходит в OpenAI или совместимый API.** Поменяйте адрес, ключ и имя модели:

```python
import os
from openai import OpenAI

client = OpenAI(base_url=os.environ["LOOMA_API_URL"],
                api_key=os.environ["LOOMA_APP_TOKEN"])
reply = client.chat.completions.create(model="Qwen3.8-Flash-Next-NVFP4",
                                       messages=[{"role": "user", "content": "Привет"}])
```

```js
import OpenAI from "openai";
const client = new OpenAI({ baseURL: process.env.LOOMA_API_URL,
                            apiKey: process.env.LOOMA_APP_TOKEN });
```

Чтобы проект работал и вне площадки, берите адрес, ключ и модель из своих переменных
с прежними значениями по умолчанию (`OPENAI_BASE_URL`, `OPENAI_API_KEY`, `MODEL`), а
на площадке задайте их из `LOOMA_*` при старте. Node.js и прокси:
[Перенос: Node.js](recipes-node.md).

**Python без OpenAI SDK.** Подойдёт SDK `loomafloat`: у него нет зависимостей, а
`looma deploy` сам положит его копию в проект, если код его импортирует.

```python
from loomafloat import Looma, client_of

looma = Looma()                                   # LOOMA_APP_TOKEN и LOOMA_API_URL из окружения
text = looma.chat("Qwen3.8-Flash-Next-NVFP4", "Придумай имя коту")
for piece in looma.chat_stream("Qwen3.8-Flash-Next-NVFP4", messages): ...
png = looma.image("Qwen-Image-2.1", "рыжий кот в скафандре", size="1024x1024")[0]
edited = looma.image("Qwen-Image-2.1", "надень шляпу", images=[png])[0]
mp4 = looma.video("MiniMax-H3", "A red fox in a snowy forest", wait=True).content()
```

**Клиенты маркетплейса.** Если в манифесте `access: clients`, записывайте вызовы
моделей на клиента: `looma.for_client(client_of(request.headers)).chat(...)`. Без SDK
передайте заголовок `X-Looma-Client` из входящего запроса в запрос к модели. Готовый
результат отметьте событием: `looma.charge("answer", client)`, имя события берётся из
раздела `events` манифеста.

## Себестоимость

Вызовы моделей из вашего сервиса записываются вам как себестоимость по прайсу
моделей на момент вызова. Её видно в статистике сервиса и в разделе «Доход» кабинета
автора. Вызовы из `looma dev` в себестоимость не идут.

## Что чем заменить

| В проекте | Замена |
|---|---|
| Ollama, vLLM, llama.cpp, LocalAI, TGI | текстовая модель сети через OpenAI-совместимый API; свой сервер модели и его контейнер в docker-compose уберите |
| `transformers` для генерации текста | текстовая модель сети |
| `diffusers`, Stable Diffusion WebUI, ComfyUI | модель изображений: генерация и правка по изображению |
| `sentence-transformers`, `FlagEmbedding`, `fastembed` | `POST /v1/embeddings` моделью типа `embedding`; длину вектора показывает `looma models` (`dimensions`) |
| cross-encoder для ре-ранка | `POST /v1/rerank` моделью типа `rerank` |
| Whisper, TTS, распознавание изображений своими моделями | модель нужного типа, если она есть в `looma models`; иначе внешний API по HTTPS с ключом в секрете; без того и другого эту часть не перенести |
| OpenAI, Anthropic и другие внешние API | можно оставить: HTTPS наружу открыт, ключ задаётся через `looma secrets set`; или перевести на модели сети |
| Веса моделей в репозитории | уберите из релиза через `.loomaignore` |

После замены уберите из зависимостей `torch`, `tensorflow`, `transformers`,
`diffusers` и подобные пакеты, если их больше ничего не использует: сборке даётся
1 ГБ памяти и 30 минут, а образ с ними весит гигабайты.

## Как ведут себя модели

- **Модель спит.** В `looma models` у неё отметка «спит». Первый вызов будит модель:
  запрос ждёт до 4 минут, затем приходит `503` с `Retry-After: 30`. Ставьте таймаут
  клиента не меньше 5 минут и повторяйте запрос после `503` через `Retry-After`.
- **429.** Модель занята, повторите позже. SDK `loomafloat` повторяет сам до трёх раз.
- **Видео.** Задание идёт минуты. Сохраните номер задания, ответьте клиенту сразу, а
  результат заберите опросом `GET /v1/videos/{id}` (статусы `queued`, `in_progress`,
  `completed`, `failed`, `cancelled`), затем `GET /v1/videos/{id}/content`. Процесс
  может уснуть, поэтому номер задания храните в базе, а не в памяти.
- **Контекст текстовой модели** показывает `looma models`. Длинные документы режьте
  на куски.
