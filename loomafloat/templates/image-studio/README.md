# {{name}}: студия изображений

Изображение по описанию (Qwen-Image-2.1) и правка любого изображения из галереи словами.
Для ИИ-агентов есть инструмент MCP `draw` (`POST /api/draw`, в ответе само изображение).
Галерея лежит в бакете сервиса (`data.bucket`) и переживает релизы и сон; у каждого
клиента каталога своя галерея.

```bash
looma deploy && looma open --env staging
looma promote
```

Локально: `pip install -r requirements.txt && looma dev`, бакет поднимется в Docker.
Или `looma dev --docker`: образ как на площадке, ставить ничего не нужно.
Без Docker запустите `looma dev --no-data`, изображения будут ложиться в `./gallery`.
