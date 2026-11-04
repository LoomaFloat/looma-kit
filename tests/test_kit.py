"""Kit для агентов: оценка проекта, проверка манифеста, запрос к приложению, установка
skills под разные агенты, MCP-инструменты и формат skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_skills
import fake_api
from loomafloat import cli, ops
from loomafloat.mcp import TOOLS, Server
from test_cli import api, login, run  # noqa: F401 — фикстура и помощники

ROOT = Path(__file__).resolve().parent.parent


def project(tmp_path: Path, files: dict) -> Path:
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def assessed(root: Path) -> dict:
    report = ops.assess(root)
    report["ids"] = {f["id"]: f["level"] for f in report["findings"]}
    return report


# ------------------------------------------------------------- оценка
def test_flask_with_sqlite_openai_and_uploads_needs_changes(tmp_path):
    root = project(tmp_path / "notes", {
        "requirements.txt": "flask==3.0\nopenai>=1.0\n",
        "app.py": ("import sqlite3\nfrom flask import Flask\nfrom openai import OpenAI\n"
                   "app = Flask(__name__)\nUPLOAD_FOLDER = 'uploads'\n"
                   "if __name__ == '__main__':\n    app.run(port=5000)\n"),
        ".env.example": "OPENAI_API_KEY=\nDATABASE_URL=\nSECRET_KEY=\n",
        "tests/test_app.py": "import redis\n",                # тесты не в счёт
    })
    report = assessed(root)
    assert report["verdict"]["code"] == "changes"
    ids = report["ids"]
    assert ids["sqlite"] == "change" and ids["local-files"] == "change"
    assert ids["listen"] == "change" and ids["external-llm-api"] == "note"
    assert "redis" not in ids
    assert report["secrets"] == ["OPENAI_API_KEY", "SECRET_KEY"]   # DATABASE_URL — платформы
    draft = report["manifest_draft"]
    assert "name: notes" in draft and "kind: python" in draft
    assert "gunicorn app:app -b 0.0.0.0:$PORT" in draft
    assert "postgres: {}" in draft and "bucket: {}" in draft and "gpu:" in draft
    where = {f["id"]: f["where"] for f in report["findings"]}
    assert "requirements.txt:2" in where["external-llm-api"]
    assert any(w.startswith("app.py:") for w in where["sqlite"])


def test_streamlit_and_local_models_are_blockers(tmp_path):
    root = project(tmp_path / "chat", {
        "requirements.txt": "streamlit\ntorch\ntransformers\nollama\n",
        "app.py": "import streamlit as st\nimport torch\nmodel.to(torch.device('cuda'))\n",
    })
    report = assessed(root)
    assert report["verdict"]["code"] == "blocked"
    ids = report["ids"]
    assert ids["streamlit"] == ids["cuda"] == ids["local-llm-server"] == "blocker"
    assert ids["heavy-ml"] == "change"


def test_node_compose_project_maps_services(tmp_path):
    package = {"name": "shop", "scripts": {"start": "node server.js"},
               "dependencies": {"express": "^4", "mongoose": "^8", "ioredis": "^5"},
               "devDependencies": {"playwright": "^1"}}
    root = project(tmp_path / "shop", {
        "package.json": json.dumps(package, indent=2),
        "server.js": ("const express = require('express');\nconst app = express();\n"
                      "app.listen(3000);\nfetch('https://api.stripe.com:8443/v1');\n"),
        "docker-compose.yml": ("services:\n  web:\n    build: .\n  admin:\n    build: ./admin\n"
                               "  mongo:\n    image: mongo:7\n  cache:\n    image: redis:7\n"
                               "  proxy:\n    image: nginx:1.27\n    privileged: true\n"),
    })
    report = assessed(root)
    ids = report["ids"]
    assert report["project"]["kind"] == "node" and "Express" in report["project"]["frameworks"]
    assert ids["mongo"] == ids["redis"] == "change"
    assert ids["privileged"] == "blocker" and ids["many-services"] == "change"
    assert ids["reverse-proxy"] == "note" and ids["outbound-ports"] == "change"
    assert "headless-browser" not in ids                  # devDependencies — инструмент тестов
    assert report["project"]["compose_services"]["mongo"] == "mongo:7"
    assert 'cmd: "npm start"' in report["manifest_draft"]


def test_a_clean_service_fits(tmp_path):
    root = project(tmp_path / "api", {
        "requirements.txt": "fastapi\nuvicorn\npsycopg[binary]\n",
        "main.py": ("import os\nfrom fastapi import FastAPI\napi = FastAPI()\n"
                    "DB = os.environ['DATABASE_URL']\n"),
    })
    report = assessed(root)
    assert report["verdict"]["code"] == "fits"
    assert set(report["ids"]) == {"postgres"}
    assert "uvicorn main:api --host 0.0.0.0 --port $PORT" in report["manifest_draft"]


def test_go_without_dockerfile_needs_one(tmp_path):
    root = project(tmp_path / "svc", {"go.mod": "module svc\n", "main.go": "package main\n"})
    report = assessed(root)
    assert report["project"]["kind"] == "docker" and report["ids"]["needs-dockerfile"] == "change"


def test_the_skill_script_runs_on_its_own(tmp_path, capsys):
    """Агент без CLI запускает скрипт прямо из папки skill."""
    import runpy
    import sys
    root = project(tmp_path / "x", {"app.py": "print(1)\n"})
    script = ROOT / "skills" / "looma-port" / "scripts" / "assess.py"
    argv = sys.argv
    sys.argv = [str(script), str(root), "--json"]
    try:
        with pytest.raises(SystemExit) as done:
            runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv = argv
    assert done.value.code == 0
    assert json.loads(capsys.readouterr().out)["verdict"]["code"] == "fits"


def test_cli_assess_prints_markdown_and_json(capsys, tmp_path):
    root = project(tmp_path / "x", {"requirements.txt": "redis\n", "app.py": "import redis\n"})
    code, out, _ = run(capsys, "assess", str(root))
    assert code == 0 and "**Итог: Подходит с правками.**" in out and "Redis" in out
    code, out, _ = run(capsys, "assess", str(root), "--json", "--out", str(tmp_path / "r.md"))
    assert code == 0 and "redis" in out and (tmp_path / "r.md").read_text().startswith("# Оценка")


# --------------------------------------------------- проверка манифеста
GOOD = "name: board\nruntime: { kind: python }\nprocesses:\n  web: { cmd: x, port: 8080 }\n"


def test_validate_reports_platform_problems_and_local_files(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    root = project(tmp_path / "board", {
        "looma.yaml": GOOD + "gpu:\n  x: { model: missing-llm }\nlisting:\n  icon: icon.png\n",
        "app.py": "from loomafloat import Looma\n"})
    code, out, err = run(capsys, "validate", str(root))
    assert code == 1 and "манифест не годится" in err
    assert "listing.icon: нет файла icon.png" in out
    assert "missing-llm" in out and "нет в каталоге" in out
    assert "looma deploy` положит её сам" in out
    (root / "icon.png").write_bytes(b"\x89PNG")
    code, out, _ = run(capsys, "validate", str(root))
    assert code == 0 and "манифест годится: есть предупреждения" in out and "архив:" in out
    (root / "looma.yaml").write_text("name: board\nruntime: { kind: python }\n")
    code, out, _ = run(capsys, "validate", str(root))
    assert code == 1 and "processes: хотя бы один процесс" in out


def test_validate_without_login_checks_what_it_can(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("LOOMA_CONFIG", str(tmp_path / "nobody.json"))
    monkeypatch.delenv("LOOMA_API_KEY", raising=False)
    root = project(tmp_path / "board", {"looma.yaml": GOOD})
    code, out, _ = run(capsys, "validate", str(root))
    assert code == 0 and "looma login" in out


def test_an_old_platform_without_the_check_is_not_an_error(api, capsys, tmp_path):
    base, state = api
    login(base, capsys)
    state.manifest_check = False
    root = project(tmp_path / "board", {"looma.yaml": GOOD})
    code, out, _ = run(capsys, "validate", str(root))
    assert code == 0 and "ошибки покажет сборка" in out
    # Старая площадка просит для проверки право manage: это тоже не ошибка.
    state.manifest_check = "forbidden"
    code, out, _ = run(capsys, "validate", str(root))
    assert code == 0 and "ошибки покажет сборка" in out


def test_deploy_stops_on_a_bad_manifest_and_shows_warnings(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    root = project(tmp_path / "board", {"looma.yaml": "name: board\nruntime: { kind: python }\n"})
    code, _out, err = run(capsys, "deploy", str(root))
    assert code == 1 and "processes" in err and ("board", 1) not in state.archives
    (root / "looma.yaml").write_text(GOOD + "gpu:\n  x: { model: missing-llm }\n")
    code, out, err = run(capsys, "deploy", str(root))
    assert code == 0, err
    assert "внимание:" in out and "missing-llm" in out


# ---------------------------------------------------------- проверка веба
def test_check_goes_through_the_owner_api(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    root = project(tmp_path / "board", {"looma.yaml": GOOD})
    monkeypatch.chdir(root)
    run(capsys, "deploy")
    code, out, _ = run(capsys, "check", "--path", "/api/health")
    assert code == 0 and "staging:/api/health: 200" in out and "staging: /api/health" in out
    code, out, err = run(capsys, "check", "--env", "prod", "--path", "/boom")
    assert code == 1 and "500" in out and "looma logs" in err
    preview = [c for c in state.calls if "/preview" in c[1]]
    assert preview[0][2] == f"Bearer {fake_api.KEY}"


def test_models_lists_kind_and_state(api, capsys):
    base, state = api
    login(base, capsys)
    state.models = [{"id": "text-big", "kind": "text", "status": "ready", "context": 32768},
                    {"id": "vectors", "kind": "embedding", "status": "cold", "dimensions": 1024}]
    code, out, _ = run(capsys, "models")
    assert code == 0
    assert "vectors" in out and "спит" in out and "вектор 1024" in out
    assert "контекст 32768" in out


# ------------------------------------------------------------ skills
@pytest.mark.parametrize("agent, home, local", [
    ("claude", None, ".claude/skills"),
    ("cursor", ".cursor/skills", ".cursor/skills"),
    ("codex", ".agents/skills", ".agents/skills"),
    ("copilot", ".copilot/skills", ".github/skills"),
    ("gemini", ".gemini/skills", ".gemini/skills"),
])
def test_skills_land_where_each_agent_reads_them(agent, home, local, capsys, tmp_path,
                                                 monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    code, out, _ = run(capsys, "skill", "install", "--agent", agent)
    assert code == 0
    user = tmp_path / ("claude-config/skills" if agent == "claude" else f"home/{home}")
    for name in ops.SKILLS:
        assert (user / name / "SKILL.md").is_file(), out
    assert (user / "looma-port" / "scripts" / "assess.py").is_file()
    assert (user / "looma-port" / "references" / "platform.md").is_file()
    code, _out, _ = run(capsys, "skill", "install", "--agent", agent,
                        "--project", str(tmp_path / "proj"))
    assert (tmp_path / "proj" / local / "looma-port" / "SKILL.md").is_file()


def test_reinstall_replaces_the_skill_folder(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    run(capsys, "skill", "install", "--agent", "agents")
    stale = tmp_path / ".agents/skills/looma-port/references/old.md"
    stale.write_text("старое")
    (tmp_path / ".agents/skills/other").mkdir()
    run(capsys, "skill", "install", "--agent", "agents")
    assert not stale.exists() and (tmp_path / ".agents/skills/other").is_dir()


def test_mcp_install_skill_still_works(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    code, out, _ = run(capsys, "mcp", "--install-skill")
    assert code == 0 and (tmp_path / "skills" / "looma-port" / "SKILL.md").is_file()


def test_repository_skills_follow_the_open_format():
    problems = [p for skill in sorted((ROOT / "skills").iterdir()) if skill.is_dir()
                for p in check_skills.check(skill)]
    assert problems == []
    assert {p.name for p in (ROOT / "skills").iterdir() if p.is_dir()} == set(ops.SKILLS)


def test_plugin_files_point_at_the_skills_and_the_server():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    servers = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]
    assert plugin["name"] == "looma" and market["plugins"][0]["name"] == "looma"
    assert market["plugins"][0]["source"] == "./" and "version" not in market["plugins"][0]
    assert servers["looma"] == {"command": "looma", "args": ["mcp"]}
    from loomafloat import __version__
    assert plugin["version"] == __version__
    for name in ops.SKILLS:
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert f'version: "{__version__}"' in text


# --------------------------------------------------------------- MCP
def test_mcp_offers_the_porting_tools(tmp_path):
    names = {t["name"] for t in TOOLS}
    assert {"looma_assess", "looma_validate", "looma_models"} <= names
    root = project(tmp_path / "x", {"requirements.txt": "pymongo\n"})
    server = Server(out=open("/dev/null", "w"))
    answer = server.call("looma_assess", {"directory": str(root)})
    report = json.loads(answer["content"][0]["text"])
    assert answer["isError"] is False and report["verdict"]["code"] == "changes"
    assert "markdown" not in report
    missing = server.call("looma_assess", {"directory": str(tmp_path / "nope")})
    assert missing["isError"] is True and "нет папки" in missing["content"][0]["text"]


def test_mcp_validate_and_check(api, capsys, tmp_path, monkeypatch):
    base, state = api
    login(base, capsys)
    root = project(tmp_path / "board", {"looma.yaml": GOOD})
    monkeypatch.chdir(root)
    run(capsys, "deploy")
    server = Server(out=open("/dev/null", "w"))
    checked = json.loads(server.call("looma_validate", {"directory": str(root)})
                         ["content"][0]["text"])
    assert checked["ok"] is True and checked["archive"]["files"] == 1
    probe = json.loads(server.call("looma_check", {"app": "board", "path": "/x"})
                       ["content"][0]["text"])
    assert probe["status"] == 200 and probe["via"] == "owner" and probe["body"] == "staging: /x"


def test_mcp_print_config_names_every_agent(capsys):
    code, out, _ = run(capsys, "mcp", "--print-config")
    assert code == 0
    for needle in ("claude mcp add", "codex mcp add", "copilot mcp add", "gemini mcp add",
                   ".vscode/mcp.json", "looma skill install"):
        assert needle in out


def test_cli_parser_knows_the_new_commands():
    actions = cli.parser()._subparsers._group_actions[0].choices
    assert {"assess", "validate", "models", "check", "skill"} <= set(actions)
