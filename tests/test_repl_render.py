"""Tests for the live-REPL renderer and Ctrl+C handling in the plain REPL.

The live REPL bug these guard against was observed on a real Windows terminal:
streamed tokens printed with end="" under prompt_toolkit's patch_stdout landed
on the prompt row and were redrawn every refresh, so the answer's last words sat
bottom-left while a blank area grew. The fix is to print WHOLE lines only.
"""
import builtins
import os

import pytest
from rich.console import Console

from gemma_cli import main as main_mod


def _events(*pairs):
    for kind, payload in pairs:
        yield (kind, payload)


# --- _consume_plain: whole lines only -------------------------------------

def test_streamed_tokens_are_printed_as_one_line(capsys):
    out = main_mod._consume_plain(_events(
        ("text", "how many "), ("text", "hours until"), ("text", " halloween?"), ("done", None),
    ))
    captured = capsys.readouterr().out
    assert captured == "how many hours until halloween?\n"
    assert out == "how many hours until halloween?"


def test_nothing_is_printed_until_a_line_is_complete(capsys):
    events = _events(("text", "still "), ("text", "streaming"))
    gen = iter(events)
    # Consume the two text events through a generator that never sends 'done'
    # and never contains a newline: nothing must have reached stdout yet.
    partial = []
    def spy():
        for e in gen:
            partial.append(e)
            yield e
    main_mod._consume_plain(spy())
    # The trailing flush at the end of the function is the ONLY output.
    assert capsys.readouterr().out == "still streaming\n"


def test_newlines_split_lines(capsys):
    main_mod._consume_plain(_events(("text", "line one\nline t"), ("text", "wo\n"), ("done", None)))
    assert capsys.readouterr().out == "line one\nline two\n"


def test_long_text_soft_wraps_at_a_space(capsys):
    text = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
    main_mod._consume_plain(_events(("text", text), ("done", None)), width=20)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > 1
    for line in lines:
        assert len(line) <= 20
        assert not line.startswith(" ")
    assert " ".join(lines) == text  # no word was split


def test_thinking_then_answer(capsys):
    main_mod._consume_plain(_events(("think", "hmm"), ("text", "answer"), ("done", None)))
    assert capsys.readouterr().out == "thinking:\nhmm\nanswer\n"


def test_thinking_hidden_when_disabled(capsys):
    main_mod._consume_plain(_events(("think", "hmm"), ("text", "answer"), ("done", None)), show_thinking=False)
    assert capsys.readouterr().out == "answer\n"


def test_tool_events_flush_pending_text(capsys):
    main_mod._consume_plain(_events(
        ("text", "Let me check"),
        ("tool_start", {"name": "read_file", "args": {"path": "a.txt"}}),
        ("tool_result", {"result": "contents"}),
        ("text", "Done."), ("done", None),
    ))
    assert capsys.readouterr().out == "Let me check\n* read_file a.txt\n  contents\nDone.\n"


def test_never_prints_a_partial_line(monkeypatch):
    """The invariant behind the fix: print() is never called with end=''."""
    calls = []
    real_print = builtins.print

    def spy(*args, **kwargs):
        calls.append(kwargs.get("end", "\n"))
        return real_print(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", spy)
    main_mod._consume_plain(_events(
        ("think", "t1"), ("think", "t2"), ("text", "a"), ("text", "b\nc"),
        ("notice", "n"), ("error", "e"), ("text", "d"), ("done", None),
    ))
    assert calls, "nothing was printed"
    assert all(end == "\n" for end in calls)


# --- _run_once: Ctrl+C stops the answer, not the session ------------------

def test_ctrl_c_mid_answer_keeps_the_session(monkeypatch):
    from gemma_cli.render import Renderer

    def interrupted_turn(cfg, messages, text, image_paths=None, approver=None, cancel=None):
        messages.append({"role": "user", "content": text})
        yield ("text", "partial ")
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "run_turn", interrupted_turn)
    console = Console(no_color=True, file=open(os.devnull, "w"))
    messages = [{"role": "system", "content": "sys"}]

    # Must not raise.
    main_mod._run_once({"model": "m"}, messages, console, Renderer(console), "hello")

    assert messages[-2]["role"] == "user"
    assert messages[-1]["role"] == "assistant"
    assert "stopped" in messages[-1]["content"]


def test_ctrl_c_does_not_duplicate_an_assistant_turn(monkeypatch):
    from gemma_cli.render import Renderer

    def interrupted_after_reply(cfg, messages, text, image_paths=None, approver=None, cancel=None):
        messages.append({"role": "user", "content": text})
        messages.append({"role": "assistant", "content": "already answered"})
        yield ("text", "already answered")
        raise KeyboardInterrupt

    monkeypatch.setattr(main_mod, "run_turn", interrupted_after_reply)
    console = Console(no_color=True, file=open(os.devnull, "w"))
    messages = [{"role": "system", "content": "sys"}]
    main_mod._run_once({"model": "m"}, messages, console, Renderer(console), "hello")
    assert [m["role"] for m in messages] == ["system", "user", "assistant"]
