# Примеры сервисов

Код четырёх сервисов из каталога Looma Float. Каждый пример можно запустить у себя,
выкатить под своим именем и взять за основу своего сервиса. «Ассистент», «Студию
изображений» и «Видеостудию» сделала команда Looma Float, «Раскадровку» написал
независимый автор.

| Папка | Что делает | Модели | События |
|---|---|---|---|
| [`assistant`](assistant/) | «Ассистент»: чат с ответом по мере генерации, инструмент MCP `ask`, API по ключу клиента | `Qwen3.8-Flash-Next-NVFP4` | `answer`: ответ |
| [`image-studio`](image-studio/) | «Студия изображений»: изображение по описанию, правка словами, галерея в бакете, инструмент MCP `draw` | `Qwen-Image-2.1` | — |
| [`video-studio`](video-studio/) | «Видеостудия»: ролик на 5 секунд по описанию, галерея в бакете | `MiniMax-H3` | `video`: ролик |
| [`storyboard`](storyboard/) | «Раскадровка»: ролик со звуком по идее в одну строку; сценарий, кадры и съёмка, инструменты MCP `make_video` и `video_status` | `Qwen3.8-Flash-Next-NVFP4`, `Qwen-Image-2.1`, `MiniMax-H3` | `video`: дубль |

Цен в примерах нет. Как добавить цену событию, написано в README каждого примера.

## Как начать

```bash
uv tool install "git+https://github.com/LoomaFloat/looma-kit"   # CLI
looma login                                                      # вход через кабинет автора
cd examples/assistant
```

Перед запуском задайте своё имя в строке `name:` файла `looma.yaml`. Имена на Looma
Float уникальны, а имена примеров заняты их сервисами в каталоге.

```bash
looma sdk && looma dev    # у себя: http://127.0.0.1:8080
looma deploy              # релиз на staging
looma promote             # релиз на prod
looma publish             # карточка и цены уходят на модерацию
```

Копии SDK (`loomafloat/`) в примерах нет. `looma deploy` кладёт её в проект сам, если
код импортирует `loomafloat`; для `looma dev` её кладёт `looma sdk`.

Сервис с нуля, шаг за шагом:
[Быстрый старт](https://docs.loomafloat.ru/authors/quickstart/).
