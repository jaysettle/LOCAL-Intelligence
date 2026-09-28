"""Tests for the shell tool's decoding and for loop detection that actually stops.

Both came from one real session: the model ran Get-Content on a .docx, the shell
tool's reader thread crashed on byte 0x9d and reported "(no output)", and loop
detection warned ten times without ever ending the turn - 25 tool calls, no
answer.
"""
import copy
import json
import os
import subprocess
import sys
import zipfile

import pytest

from gemma_cli import agent
from gemma_cli.tools import shell_tools


# --- shell: decoding ---------------------------------------------------------

class _Done:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_shell_decodes_leniently_and_isolates(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return _Done("ok\n")

    monkeypatch.setattr(shell_tools.subprocess, "run", fake_run)
    assert shell_tools.shell({"command": "Get-Date"}).strip() == "ok"
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert seen["stdin"] is subprocess.DEVNULL
    assert "text" not in seen                          # the locale decoding that crashed is gone
    if shell_tools._IS_WINDOWS:
        assert seen["argv"][-1].startswith(shell_tools._PS_UTF8_PREFIX)
        assert seen["argv"][-1].endswith("Get-Date")
        assert seen["creationflags"] & 0x08000000      # CREATE_NO_WINDOW: private console


def test_looks_binary():
    assert shell_tools._looks_binary("PK\x03\x04\x00\x00junk")
    assert shell_tools._looks_binary("\ufffd" * 50 + "abc")
    assert not shell_tools._looks_binary("Directory: C:\\Users\n\nMode  Name\n----  ----\nd---  docs\n")
    assert not shell_tools._looks_binary("")


def test_binary_output_gets_a_pointer_to_the_right_tool(monkeypatch):
    monkeypatch.setattr(shell_tools.subprocess, "run", lambda argv, **kw: _Done("PK\x03\x04\x00\x00\x14\x00"))
    out = shell_tools.shell({"command": "Get-Content x.docx"})
    assert out.startswith("Note: this output looks like binary data")
    assert "read_document" in out and "view_image" in out


def test_shell_timeout_and_failure_messages(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 5)
    monkeypatch.setattr(shell_tools.subprocess, "run", boom)
    assert "timed out" in shell_tools.shell({"command": "sleep 99", "timeout": 5})


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real PowerShell")


@windows_only
def test_real_powershell_round_trips_unicode():
    out = shell_tools.shell({"command": "'caf\u00e9 | \u2713 | \u65e5\u672c'"})
    assert "caf\u00e9 | \u2713 | \u65e5\u672c" in out


@windows_only
def test_real_powershell_binary_file_does_not_crash(tmp_path, monkeypatch):
    """The exact failure from the live session: Get-Content on a .docx."""
    monkeypatch.chdir(tmp_path)
    doc = tmp_path / "Dashboard Tags.docx"
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", "<w/>")
        z.writestr("word/media/image1.png", bytes(range(256)) * 40)
    out = shell_tools.shell({"command": f"Get-Content '{doc}'"})
    assert out != "(no output)"
    assert out.startswith("Note: this output looks like binary data")


@windows_only
def test_real_powershell_exit_codes_survive_the_prefix():
    assert "[Exit code: 3]" in shell_tools.shell({"command": "exit 3"})
    assert "[Exit code:" not in shell_tools.shell({"command": "'fine'"})


@windows_only
def test_real_powershell_stdin_is_not_the_keyboard():
    out = shell_tools.shell({"command": "$x = [Console]::In.ReadLine(); 'got:' + [string]::IsNullOrEmpty($x)",
                             "timeout": 20})
    assert "got:True" in out


# --- loop detection that stops ------------------------------------------------

class _Resp:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for c in self._chunks:
            yield json.dumps(c).encode()

    def close(self):
        pass


def _calls(*pairs):
    return [{"message": {"content": "", "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in pairs]}},
            {"done": True}]


def _text(t):
    return [{"message": {"content": t}}, {"done": True}]


def _fake(monkeypatch, script):
    sent = []

    def post(url, json=None, stream=False, timeout=None):
        sent.append(copy.deepcopy(json))
        return _Resp(script[min(len(sent) - 1, len(script) - 1)])

    monkeypatch.setattr(agent.requests, "post", post)
    return sent


BASE = {"ollama_url": "http://x", "model": "m", "num_ctx": 4096}


def _assert_well_formed(messages):
    """Every assistant tool_calls message is followed by one tool reply per call."""
    for i, m in enumerate(messages):
        calls = m.get("tool_calls") or []
        if m.get("role") == "assistant" and calls:
            replies = messages[i + 1:i + 1 + len(calls)]
            assert [r.get("role") for r in replies] == ["tool"] * len(calls), f"unanswered calls at {i}"
    assert messages[-1]["role"] == "assistant"


def test_loop_is_warned_twice_then_the_turn_ends(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = _fake(monkeypatch, [_calls(("list_directory", {"path": "."}))])   # forever the same call
    messages = [{"role": "system", "content": "s"}]
    events = list(agent.run_turn(dict(BASE), messages, "look around"))

    # calls 1-2 run, 3 and 4 are warned, 5 stops the turn: 5 requests, not 25.
    assert len(sent) == 5
    notices = [p for k, p in events if k == "notice"]
    assert sum("nudging" in n for n in notices) == 2
    assert any("stopping the turn" in n for n in notices)
    assert any(k == "text" and "kept repeating" in p for k, p in events)
    assert events[-1] == ("done", None)
    _assert_well_formed(messages)


def test_outstanding_calls_get_replies_when_the_turn_stops(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    same = ("list_directory", {"path": "."})
    _fake(monkeypatch, [_calls(same, ("glob", {"pattern": "*.x"}))])
    messages = [{"role": "system", "content": "s"}]
    list(agent.run_turn(dict(BASE), messages, "go"))
    _assert_well_formed(messages)
    assert any("Not run: the turn was stopped" in str(m.get("content")) for m in messages)


def test_max_loop_nudges_is_configurable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sent = _fake(monkeypatch, [_calls(("list_directory", {"path": "."}))])
    list(agent.run_turn(dict(BASE, max_loop_nudges=0), [{"role": "system", "content": "s"}], "go"))
    assert len(sent) == 3                              # stops at the first repeat-detection


def test_a_varied_model_is_not_stopped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = [_calls(("glob", {"pattern": f"*.{i}"})) for i in range(6)] + [_text("done")]
    sent = _fake(monkeypatch, script)
    events = list(agent.run_turn(dict(BASE), [{"role": "system", "content": "s"}], "go"))
    assert len(sent) == 7 and not any(k == "notice" and "loop" in p for k, p in events)


def test_tool_call_limit_leaves_a_well_formed_transcript(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = [_calls(("glob", {"pattern": f"*.{i}"})) for i in range(10)]
    _fake(monkeypatch, script)
    messages = [{"role": "system", "content": "s"}]
    events = list(agent.run_turn(dict(BASE), messages, "go", max_iters=3))
    assert any(k == "text" and "tool-call limit" in p for k, p in events)
    _assert_well_formed(messages)
    assert "tool-call limit" in messages[-1]["content"]
