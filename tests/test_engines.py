"""Образцы движков в `engines/`: то, что можно проверить без видеокарты."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ENGINES = Path(__file__).resolve().parent.parent / "engines"


def test_llm_engine_is_the_same_file_in_both_forms():
    """Движок кодом и движок образом несут одну службу: разойдись копии,
    сравнение двух видов запуска перестало бы что-то значить."""
    assert (ENGINES / "llm" / "llm_engine" / "server.py").read_bytes() == \
        (ENGINES / "llm-box" / "llm_engine" / "server.py").read_bytes()


def test_every_engine_folder_has_its_manifest_and_entry():
    for folder in sorted(p for p in ENGINES.iterdir() if p.is_dir()):
        manifest = folder / ("looma-extension.yaml" if folder.name == "echo-upper"
                             else "looma-engine.yaml")
        assert manifest.is_file(), folder.name
        text = manifest.read_text()
        if manifest.name == "looma-engine.yaml":
            assert f"name: {folder.name}" in text
        if "image:" in text:
            assert (folder / "Dockerfile").is_file(), folder.name
        elif "requirements:" in text:
            assert (folder / "requirements.txt").is_file(), folder.name


@pytest.fixture
def llm(monkeypatch):
    """Служба движка `llm` с подменённой моделью: договор HTTP без torch."""
    monkeypatch.setenv("LOOMA_ENGINE_OPTIONS", json.dumps({"max_tokens": 100}))
    monkeypatch.setenv("LOOMA_MODEL_ID", "alice/qwen")
    spec = importlib.util.spec_from_file_location(
        "llm_engine_server_under_test", ENGINES / "llm" / "llm_engine" / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    module.base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield module
    httpd.shutdown()
    sys.modules.pop(spec.name, None)


def _call(module, path, body=None):
    request = urllib.request.Request(module.base + path,
                                     data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, answer.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_llm_is_not_ready_until_the_model_is_loaded(llm):
    status, text = _call(llm, "/health")
    assert status == 503 and json.loads(text)["status"] == "loading"
    status, text = _call(llm, "/v1/chat/completions", {"messages": []})
    assert status == 503 and "грузится" in text
    llm.STATE.update(ready=True, device="cuda")
    status, text = _call(llm, "/health")
    assert status == 200 and json.loads(text)["status"] == "ok"
    assert json.loads(text)["model"] == "alice/qwen"


def test_llm_answers_whole_and_as_a_stream_with_usage(llm, monkeypatch):
    seen = {}

    def fake(messages, params, on_text=None):
        seen.update(messages=messages, params=params)
        for piece in ("Привет", ", ", "мир") if on_text else ():
            on_text(piece)
        return {"text": "Привет, мир", "prompt_tokens": 7, "completion_tokens": 3,
                "finish": "stop"}

    monkeypatch.setattr(llm, "generate", fake)
    llm.STATE.update(ready=True)
    ask = {"messages": [{"role": "user", "content": "скажи привет"}], "max_tokens": 5000,
           "temperature": 0}
    status, text = _call(llm, "/v1/chat/completions", ask)
    body = json.loads(text)
    assert status == 200 and body["choices"][0]["message"]["content"] == "Привет, мир"
    assert body["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert body["model"] == "alice/qwen"
    # Запрос просил больше, чем разрешила модель: предел рецепта держится.
    assert seen["params"] == {"max_new_tokens": 100, "do_sample": False, "temperature": None,
                              "top_p": None, "top_k": None}

    status, text = _call(llm, "/v1/chat/completions", {**ask, "stream": True})
    events = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    assert "".join(e["choices"][0]["delta"].get("content", "") for e in events) == "Привет, мир"
    assert events[-1]["usage"]["completion_tokens"] == 3
    assert events[-1]["choices"][0]["finish_reason"] == "stop"
    assert text.rstrip().endswith("data: [DONE]")


def test_llm_reports_a_failed_generation(llm, monkeypatch):
    def broken(messages, params, on_text=None):
        raise RuntimeError("OutOfMemoryError: CUDA out of memory")

    monkeypatch.setattr(llm, "generate", broken)
    llm.STATE.update(ready=True)
    ask = {"messages": [{"role": "user", "content": "x"}]}
    status, text = _call(llm, "/v1/chat/completions", ask)
    assert status == 500 and "out of memory" in text
    status, text = _call(llm, "/v1/chat/completions", {**ask, "stream": True})
    assert status == 200 and "out of memory" in text and "[DONE]" in text
    status, text = _call(llm, "/v1/chat/completions", {"prompt": "нет messages"})
    assert status == 400


def test_llm_request_limits(llm):
    assert llm.limits({}) == {"max_new_tokens": 100, "do_sample": True, "temperature": 0.7,
                              "top_p": 1.0}
    assert llm.limits({"max_tokens": 8, "temperature": 0}) == {
        "max_new_tokens": 8, "do_sample": False, "temperature": None, "top_p": None,
        "top_k": None}
    assert llm.limits({"max_completion_tokens": 20, "temperature": 5, "top_p": 0})["temperature"] \
        == 2.0
    assert llm.text_of([{"type": "text", "text": "a"}, {"type": "image_url"}, {"text": "b"}]) \
        == "ab"
