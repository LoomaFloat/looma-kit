# {{name}} — чат-ассистент

Страница с чатом; ответ большой модели печатается по мере генерации.

```bash
looma deploy && looma open --env staging
looma secrets set ASSISTANT_SYSTEM="Ты — вежливый юрист. Отвечай коротко."
looma promote
```

Для агентов — инструмент MCP `ask` (`POST /api/ask`, ответ целиком): после
`looma publish` его можно подключить к Claude или Cursor со страницы приложения в
маркетплейсе. Ответы клиентам маркетплейса отмечаются событием `answer`.

Модель — в `looma.yaml` (`gpu.chat`) и в `ASSISTANT_MODEL`. Локально — `looma dev` и
http://127.0.0.1:8080: модель зовётся dev-токеном приложения (дневная квота), характер —
строкой `ASSISTANT_SYSTEM=…` в `.env`.
