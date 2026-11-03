# {{name}} — студия картинок

Описание → картинка (Qwen-Image-2.1), правка любой картинки галереи словами.
Для агентов — инструмент MCP `draw` (`POST /api/draw`, ответ — сама картинка).
Галерея — в бакете приложения (`data.bucket`), она переживает релизы и сон.

```bash
looma deploy && looma open --env staging
looma promote
```

Локально: `pip install -r requirements.txt && looma dev` — бакет поднимется в Docker
(или `looma dev --docker`: образ как на платформе, ставить ничего не нужно).
Без Docker — `looma dev --no-data`, картинки ложатся в `./gallery`.
