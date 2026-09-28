"""Typed decisions (decide.py), the thinking decision in run_turn, and the loopback fix."""
import json
import math

import pytest

from gemma_cli import agent, decide as dm
from gemma_cli.config import _loopback_ipv4, load_config


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _pos(token, alts):
    return {"token": token, "logprob": 0.0,
            "top_logprobs": [{"token": t, "logprob": math.log(p)} for t, p in alts]}


def _cfg(**over):
    cfg = {"model": "gemma4:12b", "ollama_url": "http://127.0.0.1:11434", "num_ctx": 32768,
           "keep_alive": "30m", "decide_backend": "gemma", "decide_threshold": 0.8}
    cfg.update(over)
    return cfg


@pytest.fixture
def calls(monkeypatch):
    sent = []

    def install(response):
        def post(url, json=None, headers=None, timeout=None):
            sent.append({"url": url, "json": json, "headers": headers or {}})
            return response(json) if callable(response) else response
        monkeypatch.setattr(dm.requests, "post", post)
        return sent
    return install


# ---------------------------------------------------------------------------
# Local backend (gemma through Ollama)
# ---------------------------------------------------------------------------

def test_local_choice_reads_letter_after_bracket(calls):
    """gemma4 writes '(C)': position 0 is a bracket, the letter is at position 1."""
    sent = calls(FakeResp({"logprobs": [
        _pos("(", [("(", 0.99), ("C", 0.01)]),
        _pos("B", [("B", 0.9), ("A", 0.08), ("C", 0.02)]),
        _pos(")", [(")", 1.0)]),
    ]}))
    q = dm.choice("How risky?", {"read": "Read-only", "change": "Recoverable", "destroy": "Destructive"})
    d = dm.decide(_cfg(), "Remove-Item x", {"risk": q})
    a = d.answers["risk"]
    assert a.known and a.value == "change"
    assert a.probabilities == pytest.approx({"read": 0.08, "change": 0.9, "destroy": 0.02}, abs=1e-3)
    assert 0 < a.confidence < 1
    body = sent[0]["json"]
    assert body["think"] is False and body["logprobs"] is True
    assert body["options"]["num_predict"] == 3 and body["options"]["num_ctx"] == 32768
    assert "(A) Read-only" in body["messages"][0]["content"] and "(C) Destructive" in body["messages"][0]["content"]
    assert sent[0]["url"].endswith("/api/chat")


def test_local_noul_and_score(calls):
    calls(FakeResp({"logprobs": [_pos("A", [("A", 0.75), ("B", 0.25)])]}))
    a = dm.decide(_cfg(), "text", {"q": dm.noul("Is it urgent?")}).answers["q"]
    assert a.type == "noul" and a.value == pytest.approx(0.75) and a.confidence == pytest.approx(0.5)

    calls(FakeResp({"logprobs": [_pos("C", [("C", 0.5), ("B", 0.5)])]}))
    a = dm.decide(_cfg(), "text", {"q": dm.score("How angry?", ["calm", "annoyed", "furious"])}).answers["q"]
    assert a.type == "score" and a.value == pytest.approx(1.5)


def test_local_no_letter_is_unknown_not_an_exception(calls):
    calls(FakeResp({"logprobs": [_pos("Sure", [("Sure", 1.0)]), _pos(",", [(",", 1.0)])]}))
    a = dm.decide(_cfg(), "x", {"q": dm.noul("?")}).answers["q"]
    assert not a.known and "letter" in a.error


def test_local_without_logprobs_explains(calls):
    calls(FakeResp({"message": {"content": "A"}}))
    a = dm.decide(_cfg(), "x", {"q": dm.noul("?")}).answers["q"]
    assert not a.known and "0.12.11" in a.error


def test_network_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(dm.requests, "post", boom)
    d = dm.decide(_cfg(), "x", {"a": dm.noul("?"), "b": dm.noul("?")})
    assert all(not a.known and "refused" in a.error for a in d.answers.values())


def test_too_many_options_for_letters(calls):
    calls(FakeResp({}))
    q = dm.choice("pick", {f"o{i}": str(i) for i in range(30)})
    assert "2-26" in dm.decide(_cfg(), "x", {"q": q}).answers["q"].error


def test_decide_model_override(calls):
    sent = calls(FakeResp({"logprobs": [_pos("A", [("A", 1.0)])]}))
    dm.decide(_cfg(decide_model="gemma4:e4b"), "x", {"q": dm.noul("?")})
    assert sent[0]["json"]["model"] == "gemma4:e4b"


# ---------------------------------------------------------------------------
# System One backend (TypeSafe Jev or compatible)
# ---------------------------------------------------------------------------

def test_systemone_request_and_answers(calls, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    sent = calls(FakeResp({"model": "jev-1.13.0", "answers": {
        "effort": {"type": "choice", "choice": "simple", "probabilities": {"simple": 0.97, "reasoning": 0.03},
                   "confidence": 0.94},
        "urgent": {"type": "noul", "noul": 0.9},
    }}))
    cfg = _cfg(decide_backend="systemone", decide_url="https://api.typesafe.ai")
    d = dm.decide(cfg, {"request": "hi"}, {"effort": dm.THINKING_QUESTION, "urgent": dm.noul("Urgent?")})
    assert sent[0]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert sent[0]["headers"]["Authorization"] == "Bearer test-key"
    assert sent[0]["json"]["model"] == "jev-latest" and sent[0]["json"]["state"] == {"request": "hi"}
    assert d.answers["effort"].value == "simple" and d.answers["effort"].confidence == 0.94
    assert d.answers["urgent"].value == 0.9 and d.answers["urgent"].confidence == pytest.approx(0.8)
    assert len(sent) == 1                      # all questions in ONE request


def test_systemone_401_names_the_env_var(calls, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    calls(FakeResp({}, status=401))
    a = dm.decide(_cfg(decide_backend="systemone"), "x", {"q": dm.noul("?")}).answers["q"]
    assert not a.known and "TYPESAFE_API_KEY" in a.error


def test_is_remote():
    assert dm.is_remote(_cfg(decide_backend="systemone", decide_url="https://api.typesafe.ai"))
    assert not dm.is_remote(_cfg(decide_backend="systemone", decide_url="http://127.0.0.1:8000"))
    assert not dm.is_remote(_cfg(decide_backend="gemma"))


def test_key_is_never_part_of_the_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-value")
    cfg = load_config({})
    assert "secret-value" not in json.dumps(cfg, default=str)


# ---------------------------------------------------------------------------
# The thinking decision point
# ---------------------------------------------------------------------------

def _decision(value, conf, error=""):
    return dm.Decision({"effort": dm.Answer("choice", value, {}, conf, error)}, "gemma", "m", 0.5)


def test_skip_thinking_only_when_confidently_simple():
    cfg = _cfg()
    assert dm.skip_thinking(cfg, _decision("simple", 0.95))
    assert not dm.skip_thinking(cfg, _decision("simple", 0.6))
    assert not dm.skip_thinking(cfg, _decision("reasoning", 1.0))
    assert not dm.skip_thinking(cfg, _decision(None, None, "timeout"))


@pytest.fixture
def fake_turn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    seen = {}

    def core(cfg, messages, user_text, image_paths=None, approver=None, cancel=None, *, tools=None,
             max_iters=None, think=None):
        seen["think"] = think
        yield ("text", "4")
        yield ("done", None)
    monkeypatch.setattr(agent, "_run_turn", core)
    return seen


def _events(cfg, text="what is 2+2"):
    return list(agent.run_turn(cfg, [], text))


def test_mode_on_skips_thinking_for_simple(fake_turn, monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "thinking_check", lambda cfg, t: _decision("simple", 0.99))
    ev = _events(_cfg(decide_thinking="on", thinking=True))
    assert fake_turn["think"] is False
    assert any(k == "notice" and "thinking off" in p for k, p in ev)
    row = dm.read_log(5, str(tmp_path))[-1]
    assert row["answer"] == "simple" and row["thinking_used"] is False and row["finished"] is True


def test_shadow_mode_changes_nothing_but_logs(fake_turn, monkeypatch, tmp_path):
    monkeypatch.setattr(dm, "thinking_check", lambda cfg, t: _decision("simple", 0.99))
    ev = _events(_cfg(decide_thinking="shadow", thinking=True))
    assert fake_turn["think"] is None
    assert not any(k == "notice" for k, _ in ev)
    row = dm.read_log(5, str(tmp_path))[-1]
    assert row["mode"] == "shadow" and row["thinking_used"] is True and "turn_seconds" in row


def test_off_mode_asks_nothing(fake_turn, monkeypatch):
    def fail(*a):
        raise AssertionError("must not be asked")
    monkeypatch.setattr(dm, "thinking_check", fail)
    _events(_cfg(decide_thinking="off"))
    assert fake_turn["think"] is None


def test_child_runs_and_no_thinking_are_not_checked(fake_turn, monkeypatch):
    def fail(*a):
        raise AssertionError("must not be asked")
    monkeypatch.setattr(dm, "thinking_check", fail)
    list(agent.run_turn(_cfg(decide_thinking="on"), [], "x", tools=[], think=False))
    _events(_cfg(decide_thinking="on", thinking=False))


def test_unavailable_decision_keeps_thinking_and_says_so(fake_turn, monkeypatch):
    monkeypatch.setattr(dm, "thinking_check", lambda cfg, t: _decision(None, None, "connection refused"))
    ev = _events(_cfg(decide_thinking="on"))
    assert fake_turn["think"] is None
    assert any(k == "notice" and "unavailable" in p for k, p in ev)


def test_remote_backend_warns_once(fake_turn, monkeypatch):
    agent._REMOTE_WARNED.clear()
    monkeypatch.setattr(dm, "thinking_check", lambda cfg, t: _decision("reasoning", 1.0))
    cfg = _cfg(decide_thinking="shadow", decide_backend="systemone", decide_url="https://api.typesafe.ai")
    first, second = _events(cfg), _events(cfg)
    assert sum("leaves this machine" in str(p) for _, p in first) == 1
    assert not any("leaves this machine" in str(p) for _, p in second)


# ---------------------------------------------------------------------------
# localhost -> 127.0.0.1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("http://localhost:11434", "http://127.0.0.1:11434"),
    ("http://LOCALHOST:11434/", "http://127.0.0.1:11434/"),
    ("http://localhost", "http://127.0.0.1"),
    ("http://192.0.2.10:11434", "http://192.0.2.10:11434"),
    ("http://myhost.lan:11434", "http://myhost.lan:11434"),
])
def test_loopback_ipv4(url, expected):
    assert _loopback_ipv4(url) == expected


def test_config_rewrites_localhost(monkeypatch):
    monkeypatch.setenv("GEMMA_OLLAMA_URL", "http://localhost:11434")
    assert load_config({})["ollama_url"] == "http://127.0.0.1:11434"
