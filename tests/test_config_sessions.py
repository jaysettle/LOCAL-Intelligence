"""Tests for config layering and session persistence."""
import os

import pytest

from gemma_cli import config, sessions


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMMA_MODEL", raising=False)
    monkeypatch.delenv("GEMMA_NUM_CTX", raising=False)
    cfg = config.load_config()
    assert cfg["model"] == "gemma4:12b"
    assert cfg["num_ctx"] == 32768
    # cwd is always a writable root
    assert str(tmp_path) in [str(p) for p in cfg["allowed_write_roots"]]


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMMA_MODEL", "gemma4:e4b")
    monkeypatch.setenv("GEMMA_NUM_CTX", "8192")
    cfg = config.load_config()
    assert cfg["model"] == "gemma4:e4b"
    assert cfg["num_ctx"] == 8192


def test_cli_override_beats_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMMA_MODEL", "gemma4:e4b")
    cfg = config.load_config({"model": "gemma4:12b", "num_ctx": None})
    assert cfg["model"] == "gemma4:12b"  # explicit flag wins
    # None override must not clobber the default
    assert cfg["num_ctx"] == 32768


def test_global_memory_path_resolved(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = config.load_config()
    assert cfg["global_memory_file"]  # resolved, not None


def test_session_roundtrip(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    path = sessions.new_session_path()
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    sessions.save_session(path, messages, "gemma4:12b")
    assert path.exists()
    loaded = sessions.load_session(path)
    # system prompt is NOT persisted
    assert loaded == messages[1:]
    assert sessions.latest_session() == path


def test_latest_session_none_when_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert sessions.latest_session() is None


def test_compaction_triggers_and_preserves_boundary(monkeypatch):
    """Compaction summarizes old turns, keeps system + last user turn, clean boundary."""
    from gemma_cli import agent

    monkeypatch.setattr(agent, "_chat_once", lambda cfg, msgs, model: "SUMMARY")
    cfg = {"num_ctx": 100, "compact_at_ratio": 0.1, "model": "m", "ollama_url": "x"}
    big = "x" * 200
    messages = [{"role": "system", "content": "SYS"}]
    for _ in range(4):
        messages.append({"role": "user", "content": big})
        messages.append({"role": "assistant", "content": big})
        messages.append({"role": "tool", "tool_name": "t", "content": big})
    messages.append({"role": "user", "content": "latest question"})

    notice = agent._maybe_compact(cfg, messages)
    assert notice is not None
    assert messages[0]["content"] == "SYS"          # system prompt kept
    assert "SUMMARY" in messages[1]["content"]        # summary injected
    assert messages[-1]["content"] == "latest question"  # last user turn kept
    assert messages[1]["role"] == "system"            # boundary is not an orphan tool
