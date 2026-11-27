"""MCP-сервер Looma Float: сервисы из Claude Code, Cursor и других агентов.

    claude mcp add looma -- looma mcp
    # Cursor: ~/.cursor/mcp.json → {"mcpServers": {"looma": {"command": "looma", "args": ["mcp"]}}}

Протокол — JSON-RPC 2.0 построчно через stdin/stdout (транспорт stdio MCP);
в stdout — только сообщения протокола, всё остальное — в stderr. Ключ — тот
же, что у CLI (`looma login` или `LOOMA_API_KEY`).

Правило для агента — в `instructions`: проверь перед записью, проверь после
записи. Перед деплоем, переводом и откатом — `looma_status`; после — запрос
к адресу (`looma_check`) и лог, если что-то не так.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from typing import Callable, Dict, Optional

from loomafloat import __version__, config, ops
from loomafloat.client import Looma
from loomafloat.http import LoomaError
from loomafloat.pack import PackError

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """Looma Float размещает сервисы с GPU-моделями. У сервиса (аргумент app)
два окружения: staging (сюда встаёт каждый новый релиз) и prod (сюда — только
переводом).

Правила:
1. Проверь перед записью: перед looma_deploy, looma_promote, looma_rollback,
   looma_stop и looma_secrets_set вызови looma_status — пойми, что сейчас
   запущено и на каком релизе.
2. Проверь после записи: после looma_deploy открой staging через looma_check;
   после looma_promote и looma_rollback — prod. Не отвечает или ошибка —
   looma_logs нужного процесса и окружения, и только потом выводы.
3. На prod — только когда staging проверен, или когда пользователь просит
   явно. looma_destroy — только по прямой просьбе пользователя, это
   необратимо (база и бакет уходят вместе с сервисом).
4. Значения секретов не пересказывай и не пиши в файлы проекта: только
   looma_secrets_set. Процессы видят новые секреты после перезапуска.
5. Сборка не удалась — причина в log_tail результата looma_deploy; поправь
   проект и деплой снова. Модели сервис зовёт только те, что объявлены в
   gpu: манифеста looma.yaml (иначе 403).
6. Запустить у себя до деплоя — `looma dev` в терминале пользователя: окружение
   как на платформе, база и бакет в Docker, секреты — в .env проекта.
7. Перенос чужого проекта на площадку — по skill looma-port: looma_assess
   (что мешает), план пользователю, правки, looma_validate (манифест без
   релиза), looma_deploy, looma_check, отчёт. Модели для gpu: — из looma_models.
8. С чего начать с новым проектом или вопросом автора — skill looma: что перед
   тобой (сервис, движок, модель, то и другое), какие возможности площадки ему
   подходят, по какому пути идти.
9. Свой код на видеокарте (движок): looma_engine_try ставит папку на свои узлы
   автора без ревью; проверка — looma_model_add на этом движке, затем
   looma_call. В сеть и другим авторам — только после ревью (looma_engine_submit
   с папкой движка, по просьбе пользователя; GitHub не нужен, но можно).
10. Узлы: looma_node_connect даёт команду с ключом. Ключ не пересказывай; команду
   запускает пользователь на машине с видеокартой (или ты, если он попросил и
   это его машина). Делиться узлом с сетью и выбирать принимаемый код —
   только сам владелец в кабинете.
"""


def _app_prop():
    return {"type": "string", "description": "имя сервиса (как в looma.yaml) или его id"}


ENV = {"type": "string", "enum": ["prod", "staging"], "description": "окружение"}

TOOLS = [
    {"name": "looma_whoami", "description": "Кто я: учётная запись, проект, права ключа.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_apps", "description": "Мои сервисы с релизами окружений и адресами.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_status",
     "description": "Состояние сервиса: окружения (релиз, состояние, адрес), процессы, "
                    "сертификат, последние переключения. Вызывать перед любым изменением.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_init",
     "description": "Создать проект сервиса из шаблона в папке на диске.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка проекта (будет создана)"},
         "template": {"type": "string", "description": "шаблон",
                      "enum": ops.templates()},
         "name": {"type": "string", "description": "имя сервиса (латиница, цифры, дефис)"}},
         "required": ["directory"]}},
    {"name": "looma_assess",
     "description": "Оценить проект в папке: подойдёт ли площадке, что мешает (блокеры), что "
                    "переделать, с местами файл:строка; черновик looma.yaml. Ключ не нужен.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка проекта"}},
         "required": ["directory"]}},
    {"name": "looma_validate",
     "description": "Проверить looma.yaml и проект без релиза: все ошибки манифеста сразу, "
                    "модели из gpu: (есть ли в каталоге, спят ли), иконка, Dockerfile, архив. "
                    "Секунды вместо сборки.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка с looma.yaml"}},
         "required": ["directory"]}},
    {"name": "looma_models",
     "description": "Модели каталога: имя для gpu: манифеста, тип (text, image, video, "
                    "embedding, rerank), отвечает или спит, контекст, цена в копейках (за 1M "
                    "токенов, изображение или секунду видео): по ней считается себестоимость.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_model_list",
     "description": "Свои модели автора в каталоге (не модели сети: те в looma_models): имя "
                    "`автор/модель`, вид (веса, адаптер, настройка), состояние, публикация, "
                    "цена в копейках. handle — имя автора.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_model_check",
     "description": "Проверить веса с HuggingFace без запуска: формат safetensors, размер, нет "
                    "ли своего кода модели, встанет ли модель в сети сейчас. С comfy (описание "
                    "графа ComfyUI) — граф, входы, выход, веса, память и род модели. С движком автора "
                    "(engine вида автор/движок) repo нужен, только если движок работает с "
                    "весами: тогда проверяется, есть ли узлы, на которых движок встанет.",
     "inputSchema": {"type": "object", "properties": {
         "repo": {"type": "string", "description": "репозиторий: владелец/модель"},
         "weights": {"type": "string", "description": "запись весов вместо repo: автор/веса, "
                                                      "можно с @версией (looma_weights)"},
         "engine": {"type": "string", "description": "движок; по умолчанию torch"},
         "comfy": {"type": "string", "description": "граф ComfyUI как модель: путь к "
                                                    "описанию *.comfy.yaml (graph, output, "
                                                    "inputs, weights); черновик даёт "
                                                    "looma_comfy_inspect"},
         "recipe": {"type": "object", "description": "параметры движка, если нужны"}}}},
    {"name": "looma_model_add",
     "description": "Добавить свою модель в каталог. Одно из четырёх: weights или repo (свои "
                    "веса на движке площадки, свои реплики: weights называет запись весов из "
                    "looma_weights, свою или открытую; repo называет репозиторий, запись весов "
                    "заведётся сама и закрепит коммит); base и adapter (модель каталога с адаптером LoRA, "
                    "свои реплики); base и preset (настройка поверх модели каталога без своих "
                    "реплик: system, template, inputs, params у текста; prompt, inputs, params "
                    "у изображений и видео); comfy (граф ComfyUI: описание *.comfy.yaml, свои "
                    "реплики на движке comfyui; наборы узлов называет его поле extensions). Сначала модель видит только "
                    "автор. handle нужен один раз, если имя автора ещё не задано.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "имя модели: латиница, цифры, точка, дефис"},
         "repo": {"type": "string"},
         "weights": {"type": "string", "description": "запись весов: автор/веса[@версия]"},
         "from_recipe": {"type": "string", "description": "свой рецепт (looma_recipe): "
                                                          "автор/рецепт[@версия]; несёт основу "
                                                          "и настройку или движок и поля"},
         "engine": {"type": "string"},
         "comfy": {"type": "string", "description": "граф ComfyUI как модель: путь к "
                                                    "описанию *.comfy.yaml (graph, output, "
                                                    "inputs, weights); черновик даёт "
                                                    "looma_comfy_inspect"},
         "recipe": {"type": "object", "description": "параметры движка"},
         "base": {"type": "string", "description": "модель каталога из looma_models"},
         "adapter": {"type": "string", "description": "адаптер LoRA: имя или номер"},
         "preset": {"type": "object", "description": "настройка поверх base"},
         "summary": {"type": "string", "description": "одна строка о модели"},
         "always": {"type": "boolean", "description": "держать реплику всегда"},
         "own_nodes": {"type": "boolean", "description": "держать модель только на своих узлах "
                                                         "автора (раздел «Узлы» кабинета)"},
         "node": {"type": "string", "description": "модель на движке автора: только на этом "
                                                   "своём узле (looma_node_list)"},
         "handle": {"type": "string", "description": "имя автора, если ещё не задано"}},
         "required": ["name"]}},
    {"name": "looma_engine_list",
     "description": "Свои движки и расширения автора: имя `автор/движок`, вид, открыт ли "
                    "другим авторам, версии с решением ревью (pending, approved, rejected) и "
                    "причиной отказа. На одобренном движке собирают модель: looma_model_add с "
                    "engine `автор/движок`.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_engine_submit",
     "description": "Подать версию своего движка или расширения на ревью площадки. Основной "
                    "путь, как у сервиса: папка движка из рабочей среды (directory) с "
                    "looma-engine.yaml (или looma-extension.yaml) и пакетом на Python "
                    "упаковывается и уходит на ревью. По желанию вместо папки — открытый "
                    "репозиторий на GitHub и коммит (repo, commit, path). Версия ждёт "
                    "проверки; новая версия проходит ревью заново. Только по прямой просьбе "
                    "пользователя.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка движка (основной путь)"},
         "origin": {"type": "string", "description": "с папкой: ссылка https:// на код, если "
                                                     "он где-то опубликован (не проверяется)"},
         "repo": {"type": "string", "description": "по желанию вместо папки: "
                                                   "https://github.com/автор/репозиторий"},
         "commit": {"type": "string", "description": "с repo: коммит целиком, 40 знаков"},
         "path": {"type": "string", "description": "с repo: папка с манифестом, если он не "
                                                   "в корне"},
         "extension": {"type": "boolean", "description": "с repo: это расширение движка (у "
                                                         "папки вид берётся из манифеста)"},
         "fee": {"type": "object", "description": "доля автора движка с чужих вызовов, "
                                                  "копейки: {price_call} за вызов модели со "
                                                  "своим API, {price_in, price_out} за 1M "
                                                  "токенов у модели чата"},
         "own": {"type": "boolean", "description": "версия без ревью, только на свои узлы "
                                                   "(как looma_engine_try)"}}}},
    {"name": "looma_engine_code",
     "description": "Код одобренной версии любого движка автора: откуда собран, команда "
                    "запуска, зависимости, файлы; с file — один файл текстом. Его видит любой, "
                    "кто вошёл. Нужен, например, владельцу узла, который решает, какие движки "
                    "авторов принимать, или автору, который строит модель на чужом движке.",
     "inputSchema": {"type": "object", "properties": {
         "engine": {"type": "string", "description": "автор/движок"},
         "file": {"type": "string", "description": "путь файла в версии"},
         "number": {"type": "integer", "description": "номер версии; без него последняя "
                                                      "одобренная"}},
         "required": ["engine"]}},
    {"name": "looma_engine_try",
     "description": "Поставить свой движок или расширение из папки на свои узлы автора, без "
                    "ревью и без GitHub: папка с looma-engine.yaml (или looma-extension.yaml) "
                    "упаковывается и становится новой версией, одобренной сразу. Такая версия "
                    "работает только на узлах этой учётной записи и не видна другим авторам. "
                    "Дальше looma_model_add с engine `автор/движок` (или looma_model_set с "
                    "engine_version), затем looma_call. Для сети и других авторов нужен "
                    "looma_engine_submit.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка движка"}},
         "required": ["directory"]}},
    {"name": "looma_node_list",
     "description": "Свои узлы автора: на связи ли, видеокарта и свободная память, версия "
                    "агента, делится ли узел с сетью, что на нём работает, какие движки на него "
                    "встанут и почему нет, начислено за месяц.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_node_connect",
     "description": "Подключить новую свою машину с видеокартой: ключ и команда docker run. "
                    "Нужны драйвер NVIDIA, Docker и NVIDIA Container Toolkit. В команде ключ: он "
                    "показывается один раз, не пересказывай его в ответах. Узел работает "
                    "только на автора; делиться им с сетью автор решает в кабинете.",
     "inputSchema": {"type": "object", "properties": {
         "label": {"type": "string", "description": "подпись узла, например «домашний ПК»"}}}},
    {"name": "looma_node_rename",
     "description": "Поменять подпись своего узла.",
     "inputSchema": {"type": "object", "properties": {
         "node": {"type": "string", "description": "узел: имя или подпись"},
         "title": {"type": "string"}}, "required": ["node", "title"]}},
    {"name": "looma_node_disconnect",
     "description": "Отключить свой узел: ключ отзывается, работа на машине снимается. Только по "
                    "прямой просьбе пользователя; confirm повторяет имя узла.",
     "inputSchema": {"type": "object", "properties": {
         "node": {"type": "string"}, "confirm": {"type": "string"}},
         "required": ["node", "confirm"]}},
    {"name": "looma_call",
     "description": "Пробный вызов модели ключом автора: проверить свою модель или движок. "
                    "Чат: text. Изображение: image_prompt (файл пишется в out). Модель со "
                    "своим API (`автор/модель`): path, method, body, headers; async=true — "
                    "долгое вычисление заданием, ответ — номер задания для looma_job. Ответ "
                    "службы с двоичным телом пишется в out.",
     "inputSchema": {"type": "object", "properties": {
         "model": {"type": "string"}, "text": {"type": "string"},
         "image_prompt": {"type": "string"},
         "images": {"type": "array", "items": {"type": "string"},
                    "description": "с image_prompt: исходные изображения для правки (пути к "
                                   "файлам или ссылки)"},
         "path": {"type": "string", "description": "путь у службы со строкой запроса"},
         "method": {"type": "string", "description": "по умолчанию POST"},
         "body": {"description": "тело: объект уходит JSON, строка — текстом"},
         "headers": {"type": "object"},
         "async": {"type": "boolean", "description": "долгое вычисление заданием"},
         "out": {"type": "string", "description": "файл для изображения или двоичного ответа"}},
         "required": ["model"]}},
    {"name": "looma_job",
     "description": "Задание (run_…): состояние, ход в процентах, ответ службы. wait=true — "
                    "дождаться (до timeout_s), out — сохранить тело ответа в файл, cancel — "
                    "снять.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "wait": {"type": "boolean"},
         "timeout_s": {"type": "number"}, "out": {"type": "string"},
         "cancel": {"type": "boolean"}}, "required": ["id"]}},
    {"name": "looma_income",
     "description": "Доход автора за период (week, month, all): сервисы, свои модели, движки "
                    "(доли с чужих вызовов), узлы, отданные сети. Выплат пока нет: это учёт.",
     "inputSchema": {"type": "object", "properties": {
         "period": {"type": "string", "enum": ["week", "month", "all"]}}}},
    {"name": "looma_engine_share",
     "description": "Открыть свой одобренный движок или расширение другим авторам (public: "
                    "true) или закрыть (false). Только по прямой просьбе пользователя.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "public": {"type": "boolean"}},
         "required": ["name", "public"]}},
    {"name": "looma_model_set",
     "description": "Поменять свою модель: summary; price в копейках (price_call за вызов "
                    "модели со своим API, {price_in, price_out} за "
                    "1M токенов, price_image, price_second; {} убирает цену; у настройки это "
                    "надбавка к цене основы); preset (только пока настройка не опубликована); "
                    "always (держать реплику всегда или разрешить засыпать); engine_version "
                    "(перевести модель на другую версию движка автора: имя, цена и публикация "
                    "остаются, реплики заменяются сами).",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "summary": {"type": "string"},
         "price": {"type": "object"}, "preset": {"type": "object"},
         "always": {"type": "boolean"},
         "own_nodes": {"type": "boolean", "description": "true — только свои узлы, false — "
                                                         "ещё и узлы сети"},
         "engine_version": {"description": "номер одобренной версии движка автора или "
                                           "\"latest\""},
         "fields": {"type": "object", "description": "поля движка: имя и значение; null "
                                                     "убирает поле"},
         "node": {"type": "string", "description": "свой узел для модели на движке автора; "
                                                   "пустая строка убирает выбор"},
         "comfy": {"type": "string", "description": "новая ревизия графа ComfyUI: путь к "
                                                    "описанию *.comfy.yaml (graph, output, "
                                                    "inputs, weights); черновик даёт "
                                                    "looma_comfy_inspect"}},
         "required": ["name"]}},
    {"name": "looma_comfy_sets",
     "description": "Наборы узлов ComfyUI, которые модель может назвать в extensions: одобренные "
                    "расширения движка comfyui (свои и открытые) с узлами, папками весов и "
                    "форматами весов. Если узла нет ни в ядре, ни в наборах, набор подают на ревью "
                    "расширением (looma-extension.yaml с extends: comfyui).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "looma_comfy_inspect",
     "description": "Разобрать граф ComfyUI без запуска: какие узлы из ядра, какие дают наборы "
                    "узлов площадки (их ставит в extensions черновика), каких нет нигде; "
                    "узел-выход, файлы весов из загрузчиков, поля, похожие на входы модели, род "
                    "модели и что поправить. С write — записать черновик описания *.comfy.yaml "
                    "(файлы из весов редактора уже с repo, остальным repo дописывает автор). "
                    "Граф удачного прогона берётся по номеру run_…: так прогон становится моделью.",
     "inputSchema": {"type": "object", "properties": {
         "graph": {"type": "string", "description": "путь к графу в API-формате "
                                                    "(Workflow → Export (API)) или номер прогона run_… (граф прогона с "
                                                    "площадки; с write он ляжет рядом с черновиком)"},
         "write": {"type": "string", "description": "куда записать черновик описания; файл "
                                                    "не перезаписывается"}},
         "required": ["graph"]}},
    {"name": "looma_comfy_run",
     "description": "Прогнать граф ComfyUI на площадке, не заводя модель, и забрать файлы: так "
                    "проверяют, что граф работает на площадке, до looma_model_add, и отлаживают "
                    "его без своей видеокарты. Граф — API-формат или описание *.comfy.yaml "
                    "(тогда веса из него); веса — открытые файлы HuggingFace с repo, а без "
                    "них загрузчики берут файлы из весов редактора (looma_comfy_weights). Узлы — "
                    "ядро и открытые наборы (looma_comfy_sets), наборы называть не нужно. "
                    "Входные файлы, которые граф грузит по имени (LoadImage), лежащие рядом с "
                    "графом, едут сами. Пока бесплатно; у автора один прогон идёт и до пяти "
                    "ждут, прогон дольше 10 минут снимается.",
     "inputSchema": {"type": "object", "properties": {
         "graph": {"type": "string", "description": "путь к графу в API-формате или к описанию "
                                                    "*.comfy.yaml"},
         "weights": {"type": "string", "description": "с графом JSON: путь к описанию "
                                                      "*.comfy.yaml, из которого взять веса"},
         "files": {"type": "object", "description": "входные файлы: имя в графе → путь"},
         "out": {"type": "string", "description": "куда сохранить файлы прогона"},
         "wait": {"type": "boolean", "description": "ждать конца (по умолчанию да)"}},
         "required": ["graph"]}},
    {"name": "looma_comfy_runs",
     "description": "Свои прогоны графов ComfyUI (looma_comfy_run): состояние, файлы по узлам, "
                    "ошибка. С id — один прогон; с cancel — снять его.",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string", "description": "номер прогона run_…"},
         "cancel": {"type": "boolean", "description": "снять прогон id"}}}},
    {"name": "looma_weights",
     "description": "Записи весов каталога: репозиторий HuggingFace целиком или отдельные файлы "
                    "safetensors, закреплённые коммитом (у каждой версии записи свой коммит). Из "
                    "записи собирается своя модель: looma_model_add с weights. list: свои, "
                    "открытые записи других авторов и свои адаптеры; add: завести или закрепить "
                    "свою заново (новая версия, если коммит другой); show: с версиями; open: "
                    "открыть другим авторам (сразу, без проверки площадки); close: закрыть; "
                    "remove: убрать (модели на ней работают дальше). Сначала запись видит "
                    "только автор. Веса проверяются как у своих моделей: открытый репозиторий "
                    "без соглашения, safetensors, без своего кода модели, до предела размера.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "add", "show", "open", "close", "remove"],
                    "description": "list (по умолчанию), add, show, open, close, remove"},
         "ref": {"type": "string", "description": "add: владелец/репозиторий целиком или "
                                                  "владелец/репозиторий/путь/файл.safetensors; "
                                                  "остальные: автор/веса или имя своей записи"},
         "name": {"type": "string", "description": "add: имя записи; по умолчанию по "
                                                   "репозиторию"},
         "summary": {"type": "string", "description": "add: одна строка о весах"},
         "revision": {"type": "string", "description": "add: коммит, ветка или метка; без "
                                                       "него последний"},
         "folder": {"type": "string", "description": "add с файлом: папка файла в раскладке "
                                                     "ComfyUI"},
         "open": {"type": "boolean", "description": "add: сразу открыть другим авторам"}}}},
    {"name": "looma_run",
     "description": "Разовое вычисление: рецепт прямо в запросе на движке автора, который это "
                    "объявил (runs: true), на общих исполнителях движка. recipe — поля движка "
                    "(проверяются той же схемой, что у модели) или recipe_ref — запись рецепта "
                    "полей; inputs — входы прогона. Ждёт ответ (до 10 минут счёта), wait=false "
                    "даёт номер cmp_…; без engine — свои прогоны. Время карты и доля автора "
                    "движка записываются вызывающему. Предлагай, чтобы проверить рецепт до "
                    "модели или посчитать разово; для постоянных вызовов из сервиса заводи "
                    "модель (looma_model_add с тем же рецептом).",
     "inputSchema": {"type": "object", "properties": {
         "engine": {"type": "string", "description": "движок автора: автор/движок"},
         "recipe": {"type": "object", "description": "поля движка"},
         "recipe_ref": {"type": "string", "description": "запись рецепта полей вместо recipe"},
         "inputs": {"type": "object", "description": "входы прогона"},
         "files": {"type": "object", "description": "файлы для движка: имя → путь к файлу; "
                                                    "служба получит ссылки на них"},
         "wait": {"type": "boolean", "description": "ждать ответ (по умолчанию да)"}}}},
    {"name": "looma_recipe",
     "description": "Рецепты: данные для движка с версиями. Вид preset: настройка поверх "
                    "модели (base и preset: system, template, inputs, params); вид fields: "
                    "поля движка автора (engine и fields). Настройка поверх модели заводит "
                    "рецепт сама, каждая правка даёт версию. list: свои и открытые рецепты "
                    "других авторов; show: с версиями и содержимым; add: завести или новая "
                    "версия своего; copy: копия открытого рецепта у себя (пометка «основано "
                    "на»); model: своя модель из своего рецепта (в name имя модели); publish: "
                    "открыть другим авторам: через проверку площадки, рецепт увидят целиком "
                    "(только по слову автора); unpublish: закрыть; remove: убрать (модели "
                    "работают дальше). Чужой рецепт сначала копируют.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "show", "add", "copy", "model",
                                               "publish", "unpublish", "remove"],
                    "description": "list (по умолчанию), show, add, copy, model, publish, "
                                   "unpublish, remove"},
         "ref": {"type": "string", "description": "автор/рецепт или имя своего рецепта"},
         "name": {"type": "string", "description": "add: имя рецепта; copy: имя копии; model: "
                                                   "имя модели"},
         "base": {"type": "string", "description": "add: модель каталога под настройку"},
         "preset": {"type": "object", "description": "add: настройка"},
         "engine": {"type": "string", "description": "add: движок автора, автор/движок"},
         "fields": {"type": "object", "description": "add: поля движка"},
         "summary": {"type": "string", "description": "add, model: одна строка"},
         "weights": {"type": "string", "description": "model из рецепта полей: запись весов"},
         "repo": {"type": "string", "description": "model из рецепта полей: репозиторий"}}}},
    {"name": "looma_access",
     "description": "Доступы сервисов к каталогу человека: каким сервисам он разрешил "
                    "заводить черновики весов, рецептов и моделей от его имени (и, с правом "
                    "catalog.publish, отправлять их на публикацию). list: список с правами и "
                    "последним использованием; revoke: отозвать доступ по номеру, действует со "
                    "следующего запроса сервиса (только по слову человека). Разрешает человек "
                    "сам на странице витрины, отсюда разрешить нельзя.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "revoke"],
                    "description": "list (по умолчанию) или revoke"},
         "id": {"type": "integer", "description": "revoke: номер доступа из списка"}}}},
    {"name": "looma_comfy_weights",
     "description": "Веса редактора ComfyUI: свои файлы автора с HuggingFace и общий список "
                    "площадки (веса открытых моделей ComfyUI). Из них редактор заполняет "
                    "списки загрузчиков, а looma_comfy_run без весов берёт файлы, которые "
                    "грузит граф. В графе файл называют владелец/репозиторий/файл или только "
                    "именем файла, если оно одно в списках. add проверяет файл по правилам "
                    "весов модели (открытый репозиторий, safetensors или формат открытого "
                    "набора, папка ComfyUI) и закрепляет коммитом; своих до 100.",
     "inputSchema": {"type": "object", "properties": {
         "action": {"type": "string", "enum": ["list", "add", "remove"],
                    "description": "list (по умолчанию), add или remove"},
         "ref": {"type": "string", "description": "add, remove: владелец/репозиторий/путь на "
                                                  "HuggingFace; remove — или номер из списка"},
         "folder": {"type": "string", "description": "add: папка ComfyUI (checkpoints, "
                                                     "diffusion_models, loras, vae, "
                                                     "text_encoders…) или набора узлов"},
         "revision": {"type": "string", "description": "add: коммит; без него — последний"}}}},
    {"name": "looma_model_logs",
     "description": "Хвост лога реплик своей модели (свежие три запуска): почему движок не "
                    "встал, упал или отвечает ошибкой. Состояние и ошибка задачи, текст вывода "
                    "службы. Смотреть после looma_model_add или looma_engine_try, если модель "
                    "не отвечает.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "tail": {"type": "integer"}}, "required": ["name"]}},
    {"name": "looma_model_publish",
     "description": "Подать свою модель на публикацию: после проверки площадки её зовут все "
                    "авторы по её цене. Только по прямой просьбе пользователя.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                     "required": ["name"]}},
    {"name": "looma_model_unpublish",
     "description": "Снять свою модель с публикации или отозвать заявку: зовёт только автор.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                     "required": ["name"]}},
    {"name": "looma_model_delete",
     "description": "Удалить свою модель: реплики остановятся, сервисы, которые её зовут, "
                    "получат отказ. Необратимо; confirm должен повторять имя. Только по прямой "
                    "просьбе пользователя.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"}, "confirm": {"type": "string"}},
         "required": ["name", "confirm"]}},
    {"name": "looma_deploy",
     "description": "Собрать релиз из папки с looma.yaml и поставить на staging (promote=true — "
                    "и сразу на prod). Ждёт сборки; при отказе — причина и хвост лога. "
                    "Минуты.",
     "inputSchema": {"type": "object", "properties": {
         "directory": {"type": "string", "description": "папка с looma.yaml"},
         "app": {"type": "string", "description": "имя, если не то, что в looma.yaml"},
         "promote": {"type": "boolean", "default": False}},
         "required": ["directory"]}},
    {"name": "looma_releases", "description": "Релизы сервиса: номер, статус, ошибка.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_logs",
     "description": "Лог процесса: идущего — с узла, упавшего — сохранённый хвост.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "process": {"type": "string", "description": "процесс (web, worker…)"},
         "environment": ENV, "tail": {"type": "integer", "default": 100}},
         "required": ["app"]}},
    {"name": "looma_check",
     "description": "Запрос к вебу сервиса и код, время, начало ответа — проверка после "
                    "записи. По умолчанию через API владельца: доходит до процесса и при "
                    "access: clients. public=true — по публичному адресу, как гость (шлюз, "
                    "сертификат, вход). Спящий веб просыпается этим запросом (до двух минут).",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "environment": ENV,
         "path": {"type": "string", "default": "/", "description": "путь на сайте сервиса"},
         "public": {"type": "boolean", "default": False}},
         "required": ["app"]}},
    {"name": "looma_promote", "description": "На prod — релиз staging (или названный).",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "release": {"type": "integer"}}, "required": ["app"]}},
    {"name": "looma_rollback",
     "description": "Окружение (по умолчанию prod) — на прошлый релиз.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop(), "environment": ENV},
                     "required": ["app"]}},
    {"name": "looma_stop", "description": "Остановить окружение (без environment — оба).",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop(), "environment": ENV},
                     "required": ["app"]}},
    {"name": "looma_restart",
     "description": "Перезапустить процесс (подхватит новые секреты); cron — запустить сейчас.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "process": {"type": "string"}, "environment": ENV},
         "required": ["app", "process"]}},
    {"name": "looma_secrets_list", "description": "Имена секретов (значения не отдаются).",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_secrets_set",
     "description": "Поставить секреты {ИМЯ: значение}; null — убрать.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(),
         "values": {"type": "object", "additionalProperties": {"type": ["string", "null"]}}},
         "required": ["app", "values"]}},
    {"name": "looma_stats",
     "description": "Статистика сервиса за days дней: запросы по каналам, клиенты, события "
                    "charge, вызовы моделей и себестоимость по прайсу.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "days": {"type": "integer", "default": 30}}, "required": ["app"]}},
    {"name": "looma_publish",
     "description": "Опубликовать карточку сервиса в маркетплейсе: берутся разделы listing "
                    "и events (цены) из looma.yaml релиза на prod, версия уходит на "
                    "модерацию. Только по просьбе пользователя — карточку увидят все.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_listing",
     "description": "Карточка в маркетплейсе: в каталоге ли, что на модерации, причина отказа.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_unpublish",
     "description": "Снять карточку из каталога (вернуть — looma_publish, снова через "
                    "модерацию). Только по просьбе пользователя.",
     "inputSchema": {"type": "object", "properties": {"app": _app_prop()}, "required": ["app"]}},
    {"name": "looma_destroy",
     "description": "Удалить сервис со всеми данными. Необратимо: только по прямой просьбе "
                    "пользователя; confirm — имя сервиса ещё раз.",
     "inputSchema": {"type": "object", "properties": {
         "app": _app_prop(), "confirm": {"type": "string"}}, "required": ["app", "confirm"]}},
]


class Server:
    def __init__(self, looma: Optional[Looma] = None, out=None) -> None:
        self._looma = looma
        self.out = out or sys.stdout
        self._progress_token = None
        self._progress_n = 0
        self.handlers: Dict[str, Callable] = {t["name"]: getattr(self, t["name"]) for t in TOOLS}

    @property
    def looma(self) -> Looma:
        if self._looma is None:
            self._looma = Looma()
            self._looma.key = config.user_key()
        if not self._looma.key:
            raise ops.OpError("нет ключа Looma Float: `looma login` в терминале или LOOMA_API_KEY")
        return self._looma

    # -------------------------------------------------------- протокол
    def send(self, message: dict) -> None:
        self.out.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.out.flush()

    def handle(self, message: dict) -> Optional[dict]:
        method = message.get("method")
        ident = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return self._ok(ident, {
                "protocolVersion": wanted if wanted in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "looma", "version": __version__},
                "instructions": INSTRUCTIONS})
        if method == "ping":
            return self._ok(ident, {})
        if method == "tools/list":
            return self._ok(ident, {"tools": TOOLS})
        if method == "tools/call":
            return self._ok(ident, self.call(params.get("name"), params.get("arguments") or {},
                                             (params.get("_meta") or {}).get("progressToken")))
        if ident is None:              # уведомление (notifications/initialized и пр.)
            return None
        return {"jsonrpc": "2.0", "id": ident,
                "error": {"code": -32601, "message": f"метода {method!r} нет"}}

    @staticmethod
    def _ok(ident, result) -> dict:
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def call(self, name: str, arguments: dict, progress=None) -> dict:
        handler = self.handlers.get(name)
        if handler is None:
            return self._result(f"инструмента {name!r} нет", error=True)
        self._progress_token = progress
        self._progress_n = 0
        try:
            return self._result(handler(**arguments))
        except TypeError as exc:
            return self._result(f"неверные аргументы {name}: {exc}", error=True)
        except (LoomaError, ops.OpError, PackError) as exc:
            return self._result(getattr(exc, "message", None) or str(exc), error=True)
        except Exception as exc:      # агент должен увидеть причину, а не обрыв
            traceback.print_exc(file=sys.stderr)
            return self._result(f"{type(exc).__name__}: {exc}", error=True)

    @staticmethod
    def _result(value, error: bool = False) -> dict:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                                indent=2)
        return {"content": [{"type": "text", "text": text}], "isError": error}

    def _progress(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)
        if self._progress_token is not None:
            self._progress_n += 1
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": self._progress_token,
                                  "progress": self._progress_n, "message": text}})

    # -------------------------------------------------------- инструменты
    def looma_whoami(self):
        me = self.looma.whoami()
        return {k: me.get(k) for k in ("email", "role", "project_id", "project_role", "scopes")}

    def looma_apps(self):
        return [{k: a.get(k) for k in ("name", "id", "staging_release", "prod_release", "urls")}
                for a in self.looma.apps.list()]

    def looma_status(self, app: str):
        doc = self.looma.apps.get(app)
        return {"name": doc["name"], "id": doc["id"], "urls": doc.get("urls"),
                "tls": doc.get("tls"), "environments": doc.get("environments"),
                "recent_switches": (doc.get("deploys") or [])[:5],
                "releases": [{k: r.get(k) for k in ("number", "status", "error")}
                             for r in (doc.get("releases") or [])[:5]],
                "secrets": doc.get("secrets"), "models": (doc.get("token") or {}).get("models")}

    def looma_init(self, directory: str, template: str = "hello", name: Optional[str] = None):
        return ops.init(Path(directory), template, name)

    def looma_assess(self, directory: str):
        report = ops.assess(Path(directory))
        report.pop("markdown", None)
        return report

    def looma_validate(self, directory: str):
        looma = self._looma
        if looma is None:
            looma = Looma()
            looma.key = config.user_key()
        return ops.validate(looma if looma.key else None, Path(directory))

    def looma_models(self):
        keep = ("id", "kind", "status", "context", "dimensions", "owned_by", "price_in",
                "price_out", "price_image", "price_second")
        return [{k: m.get(k) for k in keep if m.get(k) is not None} for m in self.looma.models()]

    def looma_model_list(self):
        doc = self.looma.entries.mine()
        return {"handle": doc.get("handle") or "",
                "models": [ops.entry_row(e) for e in doc["services"]]}

    def looma_model_check(self, repo: str = "", engine: str = "",
                          recipe: Optional[dict] = None, comfy: str = "", weights: str = ""):
        if comfy:
            found = self.looma.entries.check({**(recipe or {}), **ops.read_comfy(comfy)})
            return {k: found.get(k) for k in ("engine", "model", "weights", "fits", "nodes_fit",
                                              "error", "recipe")}
        if not repo and not weights and "/" not in engine:
            raise ops.OpError("что проверить? weights: автор/веса, repo: владелец/модель или "
                              "comfy: описание графа")
        found = self.looma.entries.check({**(recipe or {}), **({"repo": repo} if repo else {}),
                                          **({"weights": ops.weight_ref(self.looma, weights)}
                                             if weights else {}),
                                          **({"engine": engine} if engine else {})})
        return {k: found.get(k) for k in ("engine", "model", "weights", "fits", "stages",
                                          "nodes_fit", "error")}

    def looma_model_add(self, name: str, repo: str = "", engine: str = "",
                        recipe: Optional[dict] = None, base: str = "", adapter: str = "",
                        preset: Optional[dict] = None, summary: str = "", always: bool = False,
                        handle: str = "", own_nodes: bool = False, node: str = "",
                        comfy: str = "", weights: str = "", from_recipe: str = ""):
        payload = ops.entry_payload(name=name, repo=repo, engine=engine, recipe=recipe, base=base,
                                    adapter=adapter, preset=preset, summary=summary,
                                    always=always, own=own_nodes, node=node,
                                    comfy=ops.read_comfy(comfy) if comfy else None,
                                    weights=ops.weight_ref(self.looma, weights) if weights else "",
                                    from_recipe=ops.recipe_ref(self.looma, from_recipe)
                                    if from_recipe else "")
        return self._model(ops.add_entry(self.looma, payload, handle))

    def looma_engine_list(self):
        doc = self.looma.engines.mine()
        return {"handle": doc.get("handle") or "", "engines": doc["layers"]}

    def looma_engine_submit(self, directory: str = "", repo: str = "", commit: str = "",
                            path: str = "", extension: bool = False,
                            fee: Optional[dict] = None, own: bool = False, origin: str = ""):
        self._need_handle()
        if repo or commit:
            if directory:
                raise ops.OpError("либо directory (папка движка), либо repo и commit: "
                                  "вместе не нужно")
            if not repo or not commit:
                raise ops.OpError("из GitHub нужны оба: repo и commit (40 знаков)")
            return self.looma.engines.submit(repo=repo, commit=commit, path=path,
                                             kind="extension" if extension else "engine",
                                             fee=fee, own=own)
        if not directory:
            raise ops.OpError("какую папку подать? directory: папка с looma-engine.yaml")
        data, kind, files = ops.pack_engine(Path(directory))
        made = (self.looma.engines.try_archive(data, kind=kind) if own else
                self.looma.engines.submit_archive(data, kind=kind, fee=fee, origin=origin))
        return {**made, "files": len(files)}

    def looma_engine_code(self, engine: str, file: str = "", number: Optional[int] = None):
        if file:
            return self.looma.engines.code_file(engine, file, number=number)
        return self.looma.engines.code(engine, number=number)

    def _need_handle(self) -> None:
        if not self.looma.engines.mine().get("handle"):
            raise ops.OpError("сначала задайте имя автора: с него начинаются имена движков "
                              "(looma_model_add с handle или `looma model name <имя>`)")

    def looma_engine_try(self, directory: str):
        self._need_handle()
        data, kind, files = ops.pack_engine(Path(directory))
        made = self.looma.engines.try_archive(data, kind=kind)
        return {**made, "files": len(files),
                "next": (f"версия {made.get('submitted')} без ревью, только на своих узлах. "
                         f"Модель: looma_model_add с engine {made['ref']} (или looma_model_set "
                         f"с engine_version {made.get('submitted')}), затем looma_call")}

    def looma_node_list(self):
        doc = self.looma.nodes.mine()
        keep = ("node_id", "title", "online", "pending", "accepts_tasks", "refusal", "shared",
                "gpu_name", "device", "gpus_total", "gpus_free", "vram_free_gib",
                "agent_version", "accepts", "work", "engines", "accrued", "key_id")
        return {"nodes": [{k: n.get(k) for k in keep} for n in doc["nodes"]],
                "share": doc.get("share")}

    def looma_node_connect(self, label: str = ""):
        made = self.looma.nodes.connect(label)
        return {"node_id": made.get("node_id"), "command": made["command"],
                "note": "в команде ключ: он показывается один раз, в ответах его не повторяй. "
                        "Команду запускают на машине с видеокартой (драйвер NVIDIA, Docker, "
                        "NVIDIA Container Toolkit); через минуту узел виден в looma_node_list"}

    def _node(self, name: str) -> dict:
        node = self.looma.nodes.find(name)
        if node is None:
            raise ops.OpError(f"узла {name!r} у вас нет: looma_node_list покажет свои")
        return node

    def looma_node_rename(self, node: str, title: str):
        return self.looma.nodes.rename(self._node(node)["node_id"], title)

    def looma_node_disconnect(self, node: str, confirm: str):
        found = self._node(node)
        if confirm != found["node_id"]:
            raise ops.OpError(f"confirm должен повторять имя узла ({found['node_id']})")
        self.looma.nodes.disconnect(found["key_id"])
        return {"node_id": found["node_id"], "disconnected": True}

    def looma_call(self, model: str, text: str = "", image_prompt: str = "", path: str = "",
                   method: str = "POST", body=None, headers: Optional[dict] = None,
                   out: str = "", images: Optional[list] = None, **more):
        wait_not = bool(more.pop("async", False))
        if more:
            raise TypeError(f"лишние аргументы: {', '.join(sorted(more))}")
        if image_prompt:
            target = Path(out or "image.png")
            target.write_bytes(self.looma.image(model, image_prompt, images=images or None)[0])
            return {"model": model, "image": str(target.resolve())}
        if path or body is not None or wait_not:
            answer = self.looma.run(model, path or "/", method=method, body=body,
                                    headers=headers, wait=not wait_not)
            if hasattr(answer, "doc"):
                return {"job": answer.doc, "next": f"looma_job id={answer.id} wait=true"}
            result = {"status": answer.status, "headers": answer.headers}
            kind = answer.headers.get("content-type", "")
            if out:
                Path(out).write_bytes(answer.content)
                result["saved"] = str(Path(out).resolve())
            elif kind.startswith(("text/", "application/json")) or not kind:
                result["body"] = answer.text[:20000]
            else:
                result["body"] = f"{len(answer.content)} байт {kind}: сохраните через out"
            return result
        return {"model": model, "answer": self.looma.chat(model, text or "Привет!")}

    def looma_job(self, id: str, wait: bool = False, timeout_s: float = 600.0, out: str = "",
                  cancel: bool = False):
        job = self.looma.job(id)
        if cancel:
            return job.cancel().doc
        if wait or out:
            try:
                job.wait(poll_s=5.0, timeout_s=timeout_s)
            except LoomaError as exc:
                if "не готово" in exc.message:
                    return {**job.doc, "note": f"ещё идёт; спросите позже: {exc.message}"}
                raise
        doc = dict(job.doc)
        if out and (doc.get("result") or {}).get("url"):
            Path(out).write_bytes(job.content())
            doc["saved"] = str(Path(out).resolve())
        return doc

    def looma_income(self, period: str = "month"):
        return self.looma.income(period)

    def looma_engine_share(self, name: str, public: bool):
        layer = ops.find_layer(self.looma, name)
        return self.looma.engines.share(layer["ref"], bool(public))

    def looma_model_set(self, name: str, summary: Optional[str] = None,
                        price: Optional[dict] = None, preset: Optional[dict] = None,
                        always: Optional[bool] = None, own_nodes: Optional[bool] = None,
                        engine_version=None, fields: Optional[dict] = None,
                        node: Optional[str] = None, comfy: str = ""):
        entry = ops.find_entry(self.looma, name)
        changes = {k: v for k, v in (("summary", summary), ("price", price), ("preset", preset),
                                     ("engine_version", engine_version), ("fields", fields),
                                     ("own_node", node))
                   if v is not None}
        if always is not None:
            changes.update({"min_replicas": 1, "max_replicas": max(1, entry["max_replicas"])}
                           if always else {"min_replicas": 0})
        if own_nodes is not None:
            changes["contours"] = ["own"] if own_nodes else None
        if comfy:
            found = ops.read_comfy(comfy)
            changes.update(description=found["description"], graph=found["graph"])
        if not changes:
            raise ops.OpError("что поменять? summary, price, preset, always, own_nodes, node, "
                              "fields, engine_version или comfy")
        return self._model(self.looma.entries.update(entry["id"], changes))

    def looma_comfy_sets(self):
        return {"extensions": self.looma.comfy.extensions()}

    def looma_comfy_inspect(self, graph: str, write: str = ""):
        if write and Path(write).exists():
            raise ops.OpError(f"{write} уже есть: черновик не перезаписывает файл")
        parsed, source = ops.graph_source(self.looma, graph, write)
        report = self.looma.comfy.inspect(parsed)
        if write:
            target = Path(write)
            graph_file = os.path.relpath(source.resolve(), target.resolve().parent)
            target.write_text(ops.comfy_yaml(report, graph_file), encoding="utf-8")
            report = {**report, "written": str(target)}
        return report

    def looma_comfy_run(self, graph: str, weights: str = "", files: Optional[dict] = None,
                        out: str = "", wait: bool = True):
        source = Path(graph)
        description = ""
        if source.suffix.lower() in (".yaml", ".yml"):
            found = ops.read_comfy(source)
            parsed, description = found["graph"], found["description"]
        else:
            parsed = ops.read_graph(source)
            if weights:
                description = ops.read_comfy_text(weights)
        attached = ops.run_files(parsed, source.resolve().parent, files or {})
        made = self.looma.comfy.run(parsed, description=description, files=attached)
        if not wait:
            return {**made, "files_sent": sorted(attached)}
        done = self.looma.comfy.wait_run(made["id"])
        if done.get("status") == "completed":
            saved = ops.save_run(self.looma, done, Path(out or f"comfy-{done['id'][-8:]}"))
            done = {**done, "saved": [str(path) for path in saved]}
        return {**done, "files_sent": sorted(attached)}

    def looma_comfy_runs(self, id: str = "", cancel: bool = False):  # noqa: A002 — имя поля
        if cancel:
            if not id:
                raise ops.OpError("чей прогон снять? id")
            return self.looma.comfy.cancel_run(id)
        if id:
            return self.looma.comfy.run_status(id)
        return {"runs": self.looma.comfy.runs()}

    def looma_run(self, engine: str = "", recipe: Optional[dict] = None, recipe_ref: str = "",
                  inputs: Optional[dict] = None, files: Optional[dict] = None,
                  wait: bool = True):
        if not engine:
            return {"runs": self.looma.computes()}
        attached = ops.upload_files(self.looma, [f"{k}={v}" for k, v in (files or {}).items()])
        job = self.looma.compute(engine, recipe=recipe,
                                 recipe_ref=ops.recipe_ref(self.looma, recipe_ref)
                                 if recipe_ref else "", inputs=inputs, files=attached,
                                 wait=False)
        if not wait:
            return job.doc
        try:
            job.wait(poll_s=2.0, timeout_s=900.0)
        except LoomaError:
            pass
        out = dict(job.doc)
        if (out.get("result") or {}).get("url"):
            data = job.content()
            try:
                out["content"] = json.loads(data)
            except ValueError:
                out["content"] = data[:4000].decode("utf-8", "replace")
        return out

    def looma_recipe(self, action: str = "list", ref: str = "", name: str = "", base: str = "",
                     preset: Optional[dict] = None, engine: str = "",
                     fields: Optional[dict] = None, summary: str = "", weights: str = "",
                     repo: str = ""):
        if action in ("", "list"):
            return self.looma.recipes.view()
        if action == "add":
            if not (name or ref):
                raise ops.OpError("add: нужен name")
            return self.looma.recipes.add(name or ref, base=base, preset=preset, engine=engine,
                                          fields=fields, summary=summary)
        if not ref:
            raise ops.OpError(f"{action}: нужен ref")
        full = ops.recipe_ref(self.looma, ref)
        if action == "show":
            return self.looma.recipes.get(full)
        if action == "copy":
            return self.looma.recipes.copy(full, name)
        if action == "model":
            if not name:
                raise ops.OpError("model: нужен name, имя модели")
            more = {**({"summary": summary} if summary else {}),
                    **({"weights": ops.weight_ref(self.looma, weights)} if weights else {}),
                    **({"repo": repo} if repo else {})}
            return self._model(ops.add_entry(self.looma, {"name": name, "recipe": full, **more}))
        if action == "publish":
            return self.looma.recipes.publish(full)
        if action == "unpublish":
            return self.looma.recipes.unpublish(full)
        if action == "remove":
            return self.looma.recipes.remove(full)
        raise ops.OpError(f"action {action!r}: бывает list, show, add, copy, model, publish, "
                          "unpublish, remove")

    def looma_access(self, action: str = "list", id: Optional[int] = None):  # noqa: A002
        if action in ("", "list"):
            return {"data": self.looma.access.list()}
        if action == "revoke":
            if id is None:
                raise ops.OpError("revoke: нужен id, номер доступа из списка")
            return self.looma.access.revoke(int(id))
        raise ops.OpError(f"action {action!r}: бывает list и revoke")

    def looma_weights(self, action: str = "list", ref: str = "", name: str = "",
                      summary: str = "", revision: str = "", folder: str = "",
                      open: bool = False):  # noqa: A002 — имя поля из схемы инструмента
        if action in ("", "list"):
            return self.looma.weights.view()
        if not ref:
            raise ops.OpError(f"{action}: нужен ref")
        if action == "add":
            return self.looma.weights.add(ref, name=name, summary=summary, revision=revision,
                                          folder=folder, public=open)
        full = ops.weight_ref(self.looma, ref)
        if action == "show":
            return self.looma.weights.get(full)
        if action in ("open", "close"):
            return self.looma.weights.update(full, {"open": action == "open"})
        if action == "remove":
            return self.looma.weights.remove(full)
        raise ops.OpError(f"action {action!r}: бывает list, add, show, open, close, remove")

    def looma_comfy_weights(self, action: str = "list", ref: str = "", folder: str = "",
                            revision: str = ""):
        if action in ("", "list"):
            return self.looma.comfy.weights()
        if action == "add":
            if not ref or not folder:
                raise ops.OpError("add: нужны ref (владелец/репозиторий/путь) и folder "
                                  "(checkpoints, loras, vae…)")
            return self.looma.comfy.add_weight(ref, folder, revision=revision)
        if action == "remove":
            if not ref:
                raise ops.OpError("remove: нужен ref или номер из списка")
            return self.looma.comfy.remove_weight(ref)
        raise ops.OpError(f"action {action!r}: бывает list, add, remove")

    def looma_model_logs(self, name: str, tail: int = 200):
        entry = ops.find_entry(self.looma, name)
        return self.looma.entries.logs(entry["id"], tail=tail)

    def looma_model_publish(self, name: str):
        entry = ops.find_entry(self.looma, name)
        return self._model(self.looma.entries.publish(entry["id"]))

    def looma_model_unpublish(self, name: str):
        entry = ops.find_entry(self.looma, name)
        return self._model(self.looma.entries.unpublish(entry["id"]))

    def looma_model_delete(self, name: str, confirm: str):
        entry = ops.find_entry(self.looma, name)
        if confirm != entry["label"]:
            raise ops.OpError(f"confirm должен повторять имя модели ({entry['label']}): "
                              "удаление необратимо")
        gone = self.looma.entries.delete(entry["id"])
        return {"name": entry["label"], "deleted": True,
                "dependents": gone.get("dependents", 0)}

    @staticmethod
    def _model(entry: dict) -> dict:
        row = ops.entry_row(entry)
        if entry.get("form") == "light":
            row["preset"] = entry.get("preset") or {}
        return row

    def looma_deploy(self, directory: str, app: Optional[str] = None, promote: bool = False):
        return ops.deploy(self.looma, Path(directory), app=app, promote=promote,
                          on_progress=self._progress)

    def looma_releases(self, app: str):
        return [{k: r.get(k) for k in ("number", "status", "error", "created_at")}
                for r in self.looma.apps.releases(app)]

    def looma_logs(self, app: str, process: Optional[str] = None, environment: str = "prod",
                   tail: int = 100):
        if not process:
            names = [p["process"] for p in self.looma.apps.processes(app, environment)]
            if not names:
                return (f"на {environment} нет запущенных процессов: веб мог заснуть — "
                        "looma_check разбудит его")
            process = "web" if "web" in names else names[0]
        return self.looma.apps.logs(app, process, environment=environment,
                                    tail=max(1, min(int(tail), 2000)))

    def looma_check(self, app: str, environment: str = "staging", path: str = "/",
                    public: bool = False):
        if public:
            return ops.check(ops.app_url(self.looma, app, environment), path=path)
        return ops.check_app(self.looma, app, environment, path)

    def looma_promote(self, app: str, release: Optional[int] = None):
        doc = self.looma.apps.promote(app, release)
        return {"prod_release": doc.get("prod_release"), "prod_url": (doc.get("urls") or {})
                .get("prod"), "next": "проверь prod: looma_check с environment=prod"}

    def looma_rollback(self, app: str, environment: str = "prod"):
        doc = self.looma.apps.rollback(app, environment)
        return {f"{environment}_release": doc.get(f"{environment}_release"),
                "next": f"проверь {environment}: looma_check"}

    def looma_stop(self, app: str, environment: Optional[str] = None):
        doc = self.looma.apps.stop(app, environment)
        return {"staging_release": doc.get("staging_release"),
                "prod_release": doc.get("prod_release")}

    def looma_restart(self, app: str, process: str, environment: str = "prod"):
        try:
            return self.looma.apps.restart(app, process, environment)
        except LoomaError as exc:
            if exc.status == 404 and process == "web":
                return "web спит — проснётся первым запросом (looma_check), уже с новыми секретами"
            raise

    def looma_secrets_list(self, app: str):
        return [row["name"] for row in self.looma.apps.secrets(app)]

    def looma_secrets_set(self, app: str, values: dict):
        if not isinstance(values, dict) or not values:
            raise ops.OpError("values — {ИМЯ: значение}")
        rows = self.looma.apps.set_secrets(app, values)
        return {"secrets": [row["name"] for row in rows],
                "next": "процессы увидят новые значения после looma_restart"}

    def looma_stats(self, app: str, days: int = 30):
        doc = self.looma.apps.stats(app, days=max(1, min(int(days), 90)))
        return {k: doc[k] for k in ("app", "days", "totals", "channels", "events", "models",
                                    "top_clients")}

    def looma_publish(self, app: str):
        state = ops.listing_state(self.looma.apps.publish(app))
        state["next"] = "ответ модератора — looma_listing"
        return state

    def looma_listing(self, app: str):
        return ops.listing_state(self.looma.apps.listing(app))

    def looma_unpublish(self, app: str):
        return ops.listing_state(self.looma.apps.unpublish(app))

    def looma_destroy(self, app: str, confirm: str):
        if confirm != app:
            raise ops.OpError("confirm должен повторять имя сервиса: удаление необратимо")
        self.looma.apps.delete(app)
        return f"сервис {app} удалён вместе с базой, бакетом и релизами"


def serve(stdin=None, stdout=None, looma: Optional[Looma] = None) -> None:
    server = Server(looma, stdout)
    for line in stdin or sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            server.send({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": "не JSON"}})
            continue
        batch = message if isinstance(message, list) else [message]
        for item in batch:
            answer = server.handle(item) if isinstance(item, dict) else None
            if answer is not None:
                server.send(answer)


__all__ = ["INSTRUCTIONS", "Server", "TOOLS", "serve"]
