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
| `chat(model, messages=None, **params)` | ответ модели текстом. `messages` бывает строкой (одно сообщение пользователя) или списком сообщений в формате OpenAI. Остальные параметры как в OpenAI: `temperature`, `max_tokens` и другие. [Настройку автора](own-models.md#call) с полями входа вызывают без сообщений: `chat("anna/contract-translator", input={"text": "…"})` |
| `chat_stream(model, messages, **params)` | ответ кусками по мере генерации, для чатов, где текст печатается на глазах |
| `chat_raw(model, messages, **params)` | ответ целиком, как его отдаёт API |
| `image(model, prompt="", *, images=None, n=1, size=None, **params)` | список изображений PNG байтами. С `images` (файлы, байты, ссылки) правка по ним. Поля движка, например `seed` и `num_inference_steps`, передаются как есть |
| `embed(model, texts, *, input_type=None, dimensions=None)` | векторы текстов по порядку; строка даёт список из одного вектора. `input_type`: `query` для поиска, `document` для индексации; префиксы моделей e5 и Qwen3 подставляет сервер. `dimensions` укорачивает вектор у моделей с Matryoshka |
| `rerank(model, query, documents, *, top_n=None)` | документы по убыванию оценки: список `{"index", "relevance_score"}`, где `index` указывает в `documents`; `top_n` оставляет столько лучших |
| `video(model, prompt="", *, seconds=5, wait=False, **params)` | задание на видео; с `wait=True` дождаться готового ролика. Поля как у `POST /v1/videos`: `quality`, `size`, `seed`, `conditions` и другие |
| `run(model, path="/", *, method="POST", body=None, headers=None, wait=True)` | вызов модели со своим API (движок автора, `автор/модель`): запрос уходит службе как есть, ответ `Answer` (`status`, `headers`, `content`, `text`, `json()`). `body`: байты, строка или объект (уйдёт JSON). С `wait=False` долгое вычисление: ответ `Job` |
| `job(id)` | задание по номеру (`run_…`, `cmp_…`): `status`, `progress`, `refresh()`, `wait()`, `content()`, `save(путь)`, `cancel()` |
| `compute(engine, *, recipe=None, recipe_ref="", inputs=None, files=None, wait=True)` | разовое вычисление: рецепт в запросе на движке с `runs: true` ([Рецепт в запросе](engines.md#runs)); в `files` имя и номер файла из `upload(путь)`; ответ `Job`, у ждущего готов `content()`. Токеном сервиса: только движки из раздела [compute](../skills/looma-port/references/manifest.md#compute), за счёт автора сервиса, без `files` |
| `upload(path, purpose="input")` | положить файл в хранилище площадки (`/v1/files`): ответ с номером `id` |
| `income(period="month")` | доход автора: сервисы, свои модели, доли движков, узлы (ключом автора, не токеном сервиса) |
| `models()` | модели каталога |
| `for_client(client)` | копия `Looma` с тем же ключом и адресом; её вызовы моделей записываются на клиента каталога |
| `for_person(key)` | копия `Looma` с токеном сервиса и ключом человека: её `recipes`, `weights` и `entries` действуют в каталоге человека ([Действия в каталоге человека](#person)) |
| `access` | доступы сервисов к своему каталогу: `list()`, `revoke(id)` (ключом человека, как `looma access`) |
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

## Действия в каталоге человека {#person}

Сервис с правами в разделе [account](../skills/looma-port/references/manifest.md#account)
заводит записи в каталоге человека, когда тот разрешил. Шлюз кладёт ключ человека в
каждый его запрос к сервису, сервис зовёт API своим токеном вместе с этим ключом.

```python
from loomafloat import Looma, consent_url, person_of, rights_of

looma = Looma()

def handle(request):
    person = person_of(request.headers)          # X-Looma-Account-Key или None
    if person is None:
        return redirect(consent_url("/made"))     # страница согласия площадки и обратно
    made = looma.for_person(person).recipes.add(
        "contracts", base="Qwen3.8-Flash-Next-NVFP4", preset={"system": "Ты помогаешь юристу…"})
    if "catalog.publish" in rights_of(request.headers):
        looma.for_person(person).recipes.publish(made["ref"])
```

| Функция | Что делает |
|---|---|
| `person_of(headers)` | ключ человека из запроса к сервису; живёт десять минут, свежий приходит с каждым запросом. `None`: согласия нет, человек не вошёл или отозвал доступ |
| `rights_of(headers)` | права, которые человек дал: `["catalog.draft", …]` |
| `consent_url(next="/")` | адрес согласия на адресе сервиса, `/.looma/consent?next=…`: туда ведут человека, обратно он вернётся на `next` |

Что умеет копия `for_person(key)`, с правом `catalog.draft`:

| Метод | Что делает |
|---|---|
| `recipes.view()` | `{"mine": […], "open": […], "handle"}`: рецепты человека (у каждого `ref`, `version`, `status`, `made_by` с `app` и `title`, если запись завёл сервис) и открытые рецепты авторов |
| `recipes.add(name, *, base="", preset=None, engine="", fields=None, summary="")` | черновик рецепта: настройка поверх модели (`base`, `preset` с `system`, `template`, `inputs`, `params`) или поля движка (`engine`, `fields`). Ответ с `ref` и `version`; то же имя даёт новую версию |
| `recipes.get(ref)`, `recipes.copy(ref, name="")` | рецепт с версиями; копия открытого рецепта у человека |
| `recipes.create_model(ref, name)` | модель человека из его рецепта; она спит без вызовов |
| `weights.add("владелец/репозиторий", summary=…)`, `weights.view()` | запись весов человека; другим авторам она закрыта, открыть её может он сам или сервис с правом `catalog.publish` |
| `entries.create({"name", "base", "preset"})` | модель человека: те же поля, что у `looma model add`; `min_replicas` больше 0 и цена закрыты |
| `recipes.publish(ref)`, `entries.publish(id)` | только с правом `catalog.publish`: отправить на проверку площадки |

Записи ложатся с пометкой «сделано сервисом». Ключ только для записей каталога:
вызовы моделей делайте обычным `looma`, с ключом человека они получат `403`. Отказ
различают по `LoomaError.kind`: `consent_required` (отозвали, истёк: ведите за согласием
снова) и `insufficient_rights` (закрыто правами: покажите причину). Ключ живёт десять
минут: долгую работу делайте до записи, а запись отдельным запросом. Подробно:
[account](../skills/looma-port/references/manifest.md#account). Образец:
[`examples/curator`](../examples/curator/).

## Модель со своим API

Модель на движке автора со своим API (`kinds: [api]`) сервис объявляет в `gpu` так же, как
модели сети, и зовёт методом `run`. Площадка не разбирает ни запрос, ни ответ.

```python
answer = looma.run("anna/solver", "/primes?upto=1000000")
count = answer.json()["count"]

job = looma.for_client(client).run("anna/render", "/video", body={"scene": 7}, wait=False)
save(job.id)                       # номер в базу: процесс сервиса может уснуть
...
job = looma.job(saved_id)
if job.status == "succeeded":
    mp4 = job.content()
```

Ответ службы со статусом 400 и выше SDK поднимает `LoomaError` с её текстом. Долгое
задание кончилось отказом службы: `job.doc["result"]["status"]` и тело в `content()`.
Как устроены движки и долгие вычисления: [Свой движок](engines.md#long).

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
