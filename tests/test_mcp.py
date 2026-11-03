"""MCP-сервер `looma mcp`: разговор по протоколу с настоящим процессом против
подставного API — как его видит Claude Code или Cursor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import fake_api
from loomafloat import ops

DEVKIT = Path(__file__).resolve().parents[1]


class Session:
    def __init__(self, base: str, tmp_path: Path):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(DEVKIT),
               "LOOMA_API_URL": base, "LOOMA_API_KEY": fake_api.KEY, "LOOMA_POLL_S": "0.05",
               "LOOMA_CONFIG": str(tmp_path / "none.json")}
        # Из другой папки: копия SDK в проекте не должна перекрыть пакет.
        self.proc = subprocess.Popen([sys.executable, "-m", "loomafloat.cli", "mcp"],
                                     cwd=str(tmp_path), env=env, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.next_id = 0
        self.notifications = []

    def request(self, method, params=None):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            assert line, self.proc.stderr.read()
            message = json.loads(line)
            if "id" not in message:
                self.notifications.append(message)
                continue
            assert message["id"] == self.next_id
            return message

    def notify(self, method):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def tool(self, tool_name, **arguments):
        answer = self.request("tools/call", {"name": tool_name, "arguments": arguments})["result"]
        text = answer["content"][0]["text"]
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        return answer["isError"], value

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(5)


@pytest.fixture
def session(tmp_path):
    server, state = fake_api.start()
    s = Session(f"http://127.0.0.1:{server.server_port}", tmp_path)
    yield s, state, tmp_path
    s.close()
    server.shutdown()


def test_handshake_tools_and_instructions(session):
    s, _state, _tmp = session
    hello = s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert hello["protocolVersion"] == "2025-06-18" and hello["serverInfo"]["name"] == "looma"
    assert "Проверь перед записью" in hello["instructions"]
    s.notify("notifications/initialized")            # без ответа
    assert s.request("ping")["result"] == {}
    tools = {t["name"]: t for t in s.request("tools/list")["result"]["tools"]}
    assert {"looma_status", "looma_deploy", "looma_check", "looma_promote", "looma_rollback",
            "looma_logs", "looma_secrets_set", "looma_destroy", "looma_init"} <= set(tools)
    assert tools["looma_destroy"]["inputSchema"]["required"] == ["app", "confirm"]
    assert s.request("nope")["error"]["code"] == -32601
    error, text = s.tool("looma_nope")
    assert error and "нет" in text
    old = s.request("initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert old["protocolVersion"] == "2025-06-18"


def test_agent_path_init_deploy_check_promote(session):
    s, state, tmp = session
    s.request("initialize", {"protocolVersion": "2025-03-26"})
    error, made = s.tool("looma_init", directory=str(tmp / "bot"), template="assistant", name="bot")
    assert not error and "loomafloat/client.py" in made["files"]
    s.next_id += 1
    s.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": s.next_id, "method": "tools/call",
                                   "params": {"name": "looma_deploy",
                                              "arguments": {"directory": str(tmp / "bot")},
                                              "_meta": {"progressToken": "t1"}}}) + "\n")
    s.proc.stdin.flush()
    while True:
        message = json.loads(s.proc.stdout.readline())
        if "id" in message:
            break
        s.notifications.append(message)
    result = json.loads(message["result"]["content"][0]["text"])
    assert not message["result"]["isError"]
    assert result["status"] == "ready" and result["staging_url"].startswith("https://bot-staging")
    progress = [n for n in s.notifications if n["method"] == "notifications/progress"]
    assert progress and progress[0]["params"]["progressToken"] == "t1"
    error, status = s.tool("looma_status", app="bot")
    assert not error and status["environments"][1]["release"] == 1
    error, promoted = s.tool("looma_promote", app="bot")
    assert not error and promoted["prod_release"] == 1 and "looma_check" in promoted["next"]
    error, logs = s.tool("looma_logs", app="bot")
    assert not error and "строка 1" in logs
    error, published = s.tool("looma_publish", app="bot")
    assert not error and published["pending"] == 1 and not published["in_catalog"]
    assert "looma_listing" in published["next"]
    state.apps[state.app("bot")["id"]]["listing"]["versions"][0].update(
        status="rejected", reason="нет описания")
    error, seen = s.tool("looma_listing", app="bot")
    assert not error and seen["last_decision"] == {"version": 1, "status": "rejected",
                                                   "reason": "нет описания"}
    error, done = s.tool("looma_secrets_set", app="bot", values={"TOKEN": "super-secret"})
    assert not error and done["secrets"] == ["TOKEN"] and "super-secret" not in json.dumps(done)
    error, text = s.tool("looma_destroy", app="bot", confirm="other")
    assert error and "confirm" in text and state.app("bot") is not None
    error, text = s.tool("looma_destroy", app="bot", confirm="bot")
    assert not error and state.app("bot") is None


def test_a_failed_build_gives_the_agent_the_reason(session):
    s, state, tmp = session
    s.request("initialize", {})
    s.tool("looma_init", directory=str(tmp / "bot"), name="bot")
    state.fail_build = True
    error, result = s.tool("looma_deploy", directory=str(tmp / "bot"))
    assert not error and result["status"] == "failed"
    assert "pip failed" in result["log_tail"] and "код 1" in result["error"]


def test_errors_are_results_not_crashes(session):
    s, _state, tmp = session
    s.request("initialize", {})
    error, text = s.tool("looma_status", app="ghost")
    assert error and "нет" in text
    error, text = s.tool("looma_deploy", directory=str(tmp / "empty"))
    assert error and "looma.yaml" in text
    error, text = s.tool("looma_status")
    assert error and "аргумент" in text
    assert s.request("ping")["result"] == {}          # сервер жив


def test_check_reports_status_time_and_body():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            code = 200 if self.path == "/ok" else 503
            body = f"путь {self.path}".encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    try:
        good = ops.check(base, path="/ok")
        assert good["ok"] and good["status"] == 200 and good["body"] == "путь /ok"
        bad = ops.check(base, path="broken")
        assert not bad["ok"] and bad["status"] == 503
    finally:
        server.shutdown()
    down = ops.check("http://127.0.0.1:9/", timeout=2)
    assert not down["ok"] and down["status"] == 0 and down["error"]
