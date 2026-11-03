# Looma Kit

Всё, что нужно ИИ-агенту, чтобы перенести проект на площадку [Looma](https://loomafloat.ru)
и работать с ним дальше: оценить проект, составить план, переделать код под
`looma.yaml` и модели Looma, выкатить на staging, проверить и написать отчёт.

Внутри:

| Что | Зачем |
|---|---|
| skill **`looma-port`** | перенос проекта: оценка → план → переделка → деплой → проверка → отчёт |
| skill **`looma-apps`** | работа с приложением на Looma: деплой, логи, откат, секреты, модели, статистика, публикация |
| CLI **`looma`** | все действия из терминала; на нём работают оба skill |
| MCP-сервер **`looma mcp`** | те же действия инструментами для агента (22 инструмента) |
| SDK **`loomafloat`** | вызовы моделей Looma из кода на Python, без зависимостей |

Skills сделаны в открытом формате [Agent Skills](https://agentskills.io): их читают
Claude Code, Cursor, OpenAI Codex, GitHub Copilot, Gemini CLI и другие агенты.

## Быстрый старт

```bash
uv tool install "git+https://github.com/LoomaFloat/looma-kit"   # 1. CLI
looma login                                                      # 2. вход
looma skill install --agent claude                               # 3. skills для агента
claude mcp add --transport stdio --scope user looma -- looma mcp # 4. MCP (по желанию)
```

Потом в агенте, в папке проекта: «Перенеси этот проект на Looma и выкати на staging».

Для Claude Code вместо шагов 3 и 4 — плагин (ниже): одна установка ставит и skills,
и MCP-сервер.

---

## 1. Установить CLI

Нужен Python 3.10+ и [uv](https://docs.astral.sh/uv/) или [pipx](https://pipx.pypa.io).

```bash
uv tool install "git+https://github.com/LoomaFloat/looma-kit"
```

или

```bash
pipx install "git+https://github.com/LoomaFloat/looma-kit"
```

Проверка:

```bash
looma --version
```

Обновление:

```bash
uv tool install --reinstall "git+https://github.com/LoomaFloat/looma-kit"
```

> **Агент в приложении с окном** (Cursor, VS Code) может не видеть `looma`, если
> программа запущена не из терминала. Тогда выполните `uv tool update-shell`,
> перезапустите приложение или укажите в настройке MCP полный путь — его покажет
> `which looma` (Windows: `where looma`).

## 2. Войти

```bash
looma login      # откроется браузер: подтвердите вход в консоли Looma, ключ сохранится сам
looma whoami     # проверка
```

Для CI и серверов без браузера — ключом проекта: `looma login --key <ключ>` или
переменная окружения `LOOMA_API_KEY`. Агенту ключ не нужен: CLI и MCP-сервер берут
его из `looma login`.

## 3. Подключить к агенту

Для каждого агента — две части: **skills** (как действовать) и **MCP-сервер**
(инструменты). MCP необязателен: без него агент выполняет те же действия командами
`looma` в терминале.

`looma skill install` копирует оба skill туда, где агент их читает. Без `--project` —
в домашний каталог (для всех проектов), с `--project` — в текущий проект (можно
положить в git и поделиться с командой).

| Агент | Skills | MCP |
|---|---|---|
| Claude Code | плагин или `looma skill install --agent claude` | плагин или `claude mcp add` |
| Cursor | `looma skill install --agent cursor` | `~/.cursor/mcp.json` |
| OpenAI Codex | `looma skill install --agent codex` | `codex mcp add` |
| GitHub Copilot CLI | `looma skill install --agent copilot` | `copilot mcp add` |
| VS Code (Copilot) | `looma skill install --agent copilot --project` | `.vscode/mcp.json` |
| Gemini CLI | `looma skill install --agent gemini` | `gemini mcp add` |
| Claude.ai | zip из Releases | — |
| Claude Agent SDK | папка плагина | `mcp_servers` |
| Другие агенты со skills | `looma skill install --agent agents` | команда `looma mcp` |

### Claude Code

**Плагином** — skills и MCP-сервер одной установкой. В сессии Claude Code:

```
/plugin marketplace add LoomaFloat/looma-kit
/plugin install looma@looma-kit
```

Или из терминала:

```bash
claude plugin marketplace add LoomaFloat/looma-kit
claude plugin install looma@looma-kit --scope user
```

`--scope project` — плагин для этого проекта (запись в `.claude/settings.json`, её
можно положить в git), `--scope local` — для проекта, только у вас.

Если на GitHub нет SSH-ключа и установка не проходит, перед запуском `claude`:

```bash
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1
```

Проверка: `/plugin` — плагин `looma` включён; `/mcp` — сервер `plugin:looma:looma`;
skills вызываются как `/looma:looma-port` и `/looma:looma-apps` (или агент берёт их
сам по задаче).

Обновление:

```
/plugin marketplace update looma-kit
/reload-plugins
```

или `claude plugin update looma@looma-kit`. Автообновление сторонних каталогов по
умолчанию выключено: `/plugin` → Marketplaces → `looma-kit` → Enable auto-update.

Для MCP-сервера плагина нужен установленный CLI (шаг 1): плагин запускает команду
`looma mcp`.

**Без плагина:**

```bash
looma skill install --agent claude              # ~/.claude/skills
claude mcp add --transport stdio --scope user looma -- looma mcp
```

Для одного проекта — `looma skill install --agent claude --project` (папка
`.claude/skills`) и `claude mcp add --transport stdio --scope project looma -- looma mcp`
(файл `.mcp.json` в корне проекта):

```json
{
  "mcpServers": {
    "looma": { "command": "looma", "args": ["mcp"] }
  }
}
```

### Cursor

```bash
looma skill install --agent cursor              # ~/.cursor/skills
```

В проект — `looma skill install --agent cursor --project` (`.cursor/skills`). Cursor
читает и `.agents/skills`, и `.claude/skills`.

MCP — в `~/.cursor/mcp.json` (для всех проектов) или `.cursor/mcp.json` (для проекта):

```json
{
  "mcpServers": {
    "looma": { "type": "stdio", "command": "looma", "args": ["mcp"] }
  }
}
```

Проверка из терминала: `agent mcp list`.

### OpenAI Codex (CLI и расширение IDE)

```bash
looma skill install --agent codex               # ~/.agents/skills
codex mcp add looma -- looma mcp
```

В проект — `looma skill install --agent codex --project` (`.agents/skills`). MCP можно
описать и вручную в `~/.codex/config.toml` (для проекта — `.codex/config.toml`):

```toml
[mcp_servers.looma]
command = "looma"
args = ["mcp"]
```

Проверка: `codex mcp list`.

### GitHub Copilot CLI

```bash
looma skill install --agent copilot             # ~/.copilot/skills
copilot mcp add looma -- looma mcp
```

В проект — `looma skill install --agent copilot --project` (`.github/skills`). Вручную —
`~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "looma": { "type": "local", "command": "looma", "args": ["mcp"], "tools": ["*"] }
  }
}
```

В сессии: `/mcp add` (Server Type — Local, Command — `looma mcp`), проверка —
`/mcp show looma` или `copilot mcp list`.

### VS Code (GitHub Copilot, режим агента)

```bash
looma skill install --agent copilot --project   # .github/skills
```

MCP — файл `.vscode/mcp.json` в проекте:

```json
{
  "servers": {
    "looma": { "type": "stdio", "command": "looma", "args": ["mcp"] }
  }
}
```

Для всех проектов — палитра команд → «MCP: Add Server» → Global.

### Gemini CLI

```bash
looma skill install --agent gemini              # ~/.gemini/skills
gemini mcp add -s user looma looma mcp
```

Без `-s user` сервер запишется в `.gemini/settings.json` текущего проекта. Вручную —
`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "looma": { "command": "looma", "args": ["mcp"] }
  }
}
```

Проверка: `gemini mcp list`, в сессии — `/skills`.

### Claude.ai (веб и приложение)

1. Скачайте `looma-port.zip` со страницы
   [Releases](https://github.com/LoomaFloat/looma-kit/releases/latest)
   (прямая ссылка: `https://github.com/LoomaFloat/looma-kit/releases/latest/download/looma-port.zip`).
2. Customize → Skills → загрузите zip.

В чате нет вашего терминала и CLI: доступны оценка (загрузите в чат архив проекта) и
план переноса. Переделку и деплой ведите в агенте с терминалом.

### Claude Agent SDK

Папку `looma-kit` (клон репозитория) подключить как локальный плагин: он приносит и
skills, и MCP-сервер (`looma mcp`, нужен установленный CLI). Инструменты сервера
плагина называются `mcp__plugin_looma_looma__<инструмент>`.

Python:

```python
from claude_agent_sdk import query, ClaudeAgentOptions

options = ClaudeAgentOptions(
    cwd="/path/to/project",
    plugins=[{"type": "local", "path": "/path/to/looma-kit"}],
    skills="all",
    allowed_tools=["mcp__plugin_looma_looma__*"],
)
```

TypeScript:

```ts
import { query } from "@anthropic-ai/claude-agent-sdk";

for await (const message of query({
  prompt: "Перенеси этот проект на Looma и выкати на staging",
  options: {
    cwd: process.cwd(),
    plugins: [{ type: "local", path: "/path/to/looma-kit" }],
    skills: "all",
    allowedTools: ["mcp__plugin_looma_looma__*"],
  },
})) { /* … */ }
```

### Другие агенты

Любой агент, который читает skills открытого формата из `~/.agents/skills` или
`.agents/skills`:

```bash
looma skill install --agent agents              # ~/.agents/skills
looma skill install --agent agents --project    # .agents/skills
```

MCP-сервер — команда `looma` с аргументом `mcp` (stdio). Готовые настройки для
популярных агентов печатает:

```bash
looma mcp --print-config
```

### Без CLI: только skill

Skill можно взять и без установки CLI — тогда агент сделает оценку и план, а для
деплоя попросит поставить CLI:

```bash
git clone https://github.com/LoomaFloat/looma-kit
cp -R looma-kit/skills/looma-port looma-kit/skills/looma-apps ~/.agents/skills/
```

(вместо `~/.agents/skills` — папка skills вашего агента из таблицы выше).

---

## 4. Как пользоваться

В агенте, открытом в папке проекта:

| Что сказать | Что сделает агент |
|---|---|
| «Оцени, можно ли перенести этот проект на Looma» | оценка: что мешает, что переделать, какие модели нужны; без правок |
| «Перенеси этот проект на Looma и выкати на staging» | оценка → план (ждёт вашего согласия) → правки → проверка манифеста → staging → проверка → отчёт `LOOMA_PORT.md` |
| «Проверь staging и покажи логи» | `looma check`, `looma logs` |
| «Переведи на prod» | `looma promote` — только по такой прямой просьбе |
| «Опубликуй в маркетплейсе» | `looma publish` — карточка уходит на модерацию |

Агент не переводит на prod, не публикует и не удаляет приложение без прямой просьбы.
Значения секретов он не пишет в файлы и не повторяет: их задаёте вы сами командой
`looma secrets set ИМЯ=значение`.

Оценку можно запустить и самому — она ничего не ставит и не меняет:

```bash
looma assess path/to/project                      # отчёт в терминал
looma assess path/to/project --out report.md      # и в файл
python3 skills/looma-port/scripts/assess.py path/to/project   # без установки CLI
```

## Команды CLI

| Команда | Что делает |
|---|---|
| `looma assess [папка]` | оценка проекта для переноса; ключ не нужен |
| `looma validate [папка]` | проверка `looma.yaml` и проекта без релиза |
| `looma models` | модели каталога: имя, тип, отвечает или спит |
| `looma deploy [папка]` | релиз на staging (`--promote` — сразу на prod) |
| `looma check [--env staging] [--path /]` | запрос к приложению: код, время, начало ответа |
| `looma status` · `looma logs [процесс] [--env] [-f]` | состояние и логи |
| `looma promote` · `looma rollback` | staging → prod, откат |
| `looma secrets set ИМЯ=значение` | секреты процессов |
| `looma publish` · `looma listing` | карточка в маркетплейсе и её модерация |
| `looma dev` | запуск у себя: база и бакет в Docker, модели настоящие |
| `looma skill install` · `looma mcp` | skills и MCP-сервер для агентов |

Полный список — `looma --help` и [docs/cli.md](docs/cli.md).

## Инструменты MCP

`looma_whoami`, `looma_apps`, `looma_status`, `looma_assess`, `looma_validate`,
`looma_models`, `looma_init`, `looma_deploy`, `looma_releases`, `looma_logs`,
`looma_check`, `looma_promote`, `looma_rollback`, `looma_stop`, `looma_restart`,
`looma_secrets_list`, `looma_secrets_set`, `looma_stats`, `looma_publish`,
`looma_listing`, `looma_unpublish`, `looma_destroy`.

## Что умеет площадка

Хостинг Python, Node.js и любого стека по Dockerfile; веб, фоновые процессы и задачи
по расписанию; Postgres и S3-бакет; домен с TLS; модели сети Looma по API. Чего нет:
GPU в процессе, WebSocket, протоколов кроме HTTP, Redis и других баз кроме Postgres,
выхода наружу на порты кроме 80 и 443. Подробно, с пределами и заменами, —
[skills/looma-port/references/platform.md](skills/looma-port/references/platform.md).

## Устройство репозитория

```
looma-kit/
├── .claude-plugin/          плагин и каталог для Claude Code
├── .mcp.json                MCP-сервер плагина: команда `looma mcp`
├── skills/
│   ├── looma-port/          SKILL.md, references/, scripts/assess.py
│   └── looma-apps/          SKILL.md
├── loomafloat/              CLI, SDK и MCP-сервер (пакет Python)
├── docs/cli.md              справка по CLI и SDK
└── tests/
```

## Разработка

```bash
uv run --with pytest python -m pytest tests     # тесты
claude plugin validate .                         # проверка плагина и skills
claude --plugin-dir .                            # плагин из этой папки, без установки
```

Выпуск: поднять версию в `pyproject.toml`, `loomafloat/__init__.py`,
`.claude-plugin/plugin.json` и `metadata.version` обоих `SKILL.md`, поставить тег
`vX.Y.Z` — GitHub Actions соберёт `looma-port.zip` и `looma-apps.zip` и приложит их к
релизу. Пользователи плагина получают обновление, когда меняется версия в
`plugin.json`.

## Лицензия

MIT — см. [LICENSE](LICENSE).
