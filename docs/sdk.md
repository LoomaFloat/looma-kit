# SDK loomafloat

SDK `loomafloat` вызывает модели сети Looma Float из кода сервиса на Python и отмечает
действия клиентов каталога. Зависимостей нет, нужен Python 3.10 или новее.

## Установка

В `requirements.txt` сервиса:

```text
loomafloat @ git+https://github.com/LoomaFloat/looma-kit
```

Шаблонам с моделями `looma init` кладёт копию SDK в папку `loomafloat/` проекта, и
сборка на площадке не ходит за ним в PyPI. Обновить копию: `looma sdk`. Если код
импортирует `loomafloat`, а копии нет, `looma deploy` положит её сам. Команды описаны в
справке [CLI](cli.md).

## Класс `Looma`

```python
from loomafloat import Looma

looma = Looma()
text = looma.chat("Qwen3.8-Flash-Next-NVFP4", "Придумай имя коту")
png = looma.image("Qwen-Image-2.1", "рыжий кот в скафандре", size="1024x1024")[0]
edit = looma.image("Qwen-Image-2.1", "надень на кота шляпу", images=["cat.png"])[0]
vectors = looma.embed("bge-m3", ["первый кусок", "второй кусок"], input_type="document")
best = looma.rerank("bge-reranker-v2-m3", "вопрос", ["кусок 1", "кусок 2"], top_n=1)
mp4 = looma.video("MiniMax-H3", "A red fox in a snowy forest", quality="fast").content()
```

`Looma()` берёт адрес и токен из `LOOMA_API_URL` и `LOOMA_APP_TOKEN`: их кладёт в
окружение процесса площадка. Внутри процесса `LOOMA_API_URL` уже оканчивается на `/v1`.
Вне сервиса `Looma()` берёт ключ из `LOOMA_API_KEY` или из `looma login`. Адрес и ключ
можно передать явно: `Looma(api_url=…, key=…)`.

Сервис вызывает только модели, объявленные в разделе `gpu` манифеста
[looma.yaml](../skills/looma-port/references/manifest.md). Вызов другой модели ответит
`403`. Имена моделей показывает `looma models`.

| Метод | Что делает |
|---|---|
| `chat(model, messages, **params)` | ответ модели текстом. `messages` бывает строкой (одно сообщение пользователя) или списком сообщений в формате OpenAI. Остальные параметры как в OpenAI: `temperature`, `max_tokens` и другие |
| `chat_stream(model, messages, **params)` | ответ кусками по мере генерации, для чатов, где текст печатается на глазах |
| `chat_raw(model, messages, **params)` | ответ целиком, как его отдаёт API |
| `image(model, prompt, *, images=None, n=1, size=None, **params)` | список изображений PNG байтами. С `images` (файлы, байты, ссылки) правка по ним. Поля движка, например `seed` и `num_inference_steps`, передаются как есть |
| `embed(model, texts, *, input_type=None, dimensions=None)` | векторы текстов по порядку; строка даёт список из одного вектора. `input_type`: `query` для поиска, `document` для индексации; префиксы моделей e5 и Qwen3 подставляет сервер. `dimensions` укорачивает вектор у моделей с Matryoshka |
| `rerank(model, query, documents, *, top_n=None)` | документы по убыванию оценки: список `{"index", "relevance_score"}`, где `index` указывает в `documents`; `top_n` оставляет столько лучших |
| `video(model, prompt, *, seconds=5, wait=False, **params)` | задание на видео; с `wait=True` дождаться готового ролика. Поля как у `POST /v1/videos`: `quality`, `size`, `seed`, `conditions` и другие |
| `models()` | модели каталога |
| `for_client(client)` | копия `Looma` с тем же ключом и адресом; её вызовы моделей записываются на клиента каталога |
| `charge(event, client=None, *, quantity=1, id=None)` | отметить событие из раздела `events` для клиента |

Ответ чата, изображения, векторы и оценки SDK ждёт до 15 минут: спящую модель будит первый вызов.

## Клиенты каталога

Клиент, вошедший на витрине или пришедший с ключом клиента, приходит к сервису с
заголовком `X-Looma-Client`. Это псевдоним, свой у каждого сервиса; почты и имени
сервис не получает. Заголовок ставит шлюз площадки.

```python
from loomafloat import Looma, client_of

looma = Looma()
client = client_of(request.headers)     # псевдоним из X-Looma-Client или None
answer = looma.for_client(client).chat("Qwen3.8-Flash-Next-NVFP4", question)  # вызов записан на клиента
looma.charge("answer", client, id=request_id)   # событие из раздела events
```

`client_of` возвращает `None`, если пришёл не клиент каталога.

```yaml
events:
  - { name: answer, title: Ответ, price: 2 }   # цена события в рублях; без цены событие бесплатное
```

`charge` записывает событие на клиента, а если у события есть цена, то и расход
клиента, и доход автора; `quantity` умножает цену. Цена берётся из одобренной карточки:
новая цена действует после `looma publish` и модерации. Пополнений и выплат пока нет,
поэтому `charge` не отказывает из-за баланса. С `id="…"` повтор не запишется дважды. Так
делают шаблоны `assistant` и `video-studio`.

Без клиента, с чужим клиентом и для события, которого нет в `events` релиза, `charge`
отвечает ошибкой `LoomaError`.

Без SDK событие отмечает `POST $LOOMA_API_URL/charge` с телом
`{"client", "event", "quantity", "id"}` и токеном сервиса в `Authorization: Bearer`.

Подробно о ценах и доходе: [Клиенты и цены](https://docs.loomafloat.ru/authors/prices/).

### Только для клиентов

`access: clients` в `looma.yaml` открывает сервис только клиентам. Посетителя без входа
шлюз сам отправляет войти в каталог: человеку показывает страницу со ссылкой, программе
отвечает `401`. В запросах через шлюз `client_of` тогда не `None`. На карточке витрина
пишет «Доступ: после входа». По умолчанию `access: public`: сервис открыт всем.

### Каналы

Кроме веба, сервис может объявить каналы в разделе `channels`. Они видны на карточке в
каталоге.

```yaml
channels:
  api: { docs: /docs }                     # API по ключу клиента
  mcp:                                     # инструменты для Claude, Cursor и других агентов
    tools:
      - { name: ask, description: Ответить на вопрос, path: /api/ask,
          input: { type: object, properties: { question: { type: string } }, required: [question] } }
```

MCP-сервер сервиса находится по адресу `https://api.loomafloat.ru/mcp/<имя>`, клиенты
подключаются к нему со своим ключом. Вызов инструмента приходит как POST на его `path` с
`X-Looma-Client`. Подробно: [Каналы](https://docs.loomafloat.ru/authors/channels/).

## Видео

`video()` возвращает задание. У задания есть `id`, `status` и методы `refresh()`,
`wait()`, `content()`, `save(path)`.

```python
job = looma.video("MiniMax-H3", "A red fox in a snowy forest")
job.wait().save("fox.mp4")
```

`wait()` опрашивает задание раз в 15 секунд и ждёт до трёх часов. `content()` и `save()`
сами дождутся готового ролика. Если задание не удалось, `wait()` поднимает `LoomaError`
с причиной.

## Ошибки

Ошибка API приходит исключением `LoomaError` с кодом и причиной словами:
`exc.status`, `exc.message`. На `429` SDK ждёт `Retry-After` и повторяет запрос сам, до
трёх раз.
