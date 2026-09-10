"""Tests for the plain-REPL status bar and the whole-line / markup-safe Renderer.

Two real-terminal bugs sit behind these:
- streamed tokens flushed as partial lines under a bottom-anchored widget land on
  the widget's row (the --live screenshot), so a Live must only see whole lines;
- rich treats model text as markup, and a token ending in a backslash escapes the
  tag after it: a Windows path streamed as `[dim]C:\\[/dim][dim]Users[/dim]...`
  rendered as `C:[/dim]Users[/dim]jsettle`.
"""
import io
import os

import pytest
from rich.console import Console

from gemma_cli import main as main_mod
from gemma_cli import statusline
from gemma_cli.render import LineBuffer, Renderer
from gemma_cli.statusline import Sample, StatusBar


def _console(width=60, terminal=False):
    return Console(file=io.StringIO(), width=width, force_terminal=terminal,
                   no_color=True, color_system=None, highlight=False)


def _out(console) -> str:
    return console.file.getvalue()


def _events(*pairs):
    for kind, payload in pairs:
        yield (kind, payload)


# --- LineBuffer -----------------------------------------------------------

def test_linebuffer_holds_until_newline():
    lb = LineBuffer(width=100)
    assert lb.push("how many ") == []
    assert lb.push("hours") == []
    assert lb.push(" until halloween?\n") == ["how many hours until halloween?"]
    assert lb.flush() is None


def test_linebuffer_flush_returns_remainder():
    lb = LineBuffer(width=100)
    lb.push("tail")
    assert lb.flush() == "tail"
    assert lb.flush() is None


def test_linebuffer_soft_wraps_at_last_space():
    lb = LineBuffer(width=20)
    lines = lb.push("alpha beta gamma delta epsilon zeta eta theta")
    rest = lb.flush()
    everything = lines + ([rest] if rest else [])
    assert len(everything) > 1
    assert all(len(line) <= 20 and not line.startswith(" ") for line in everything)
    assert " ".join(everything) == "alpha beta gamma delta epsilon zeta eta theta"


def test_linebuffer_hard_cuts_a_single_giant_word():
    lb = LineBuffer(width=20)
    lines = lb.push("x" * 45)
    assert lines == ["x" * 20, "x" * 20]
    assert lb.flush() == "x" * 5


# --- Renderer: whole-line mode --------------------------------------------

def test_whole_lines_prints_one_line_for_a_streamed_sentence():
    console = _console()
    Renderer(console, whole_lines=True).consume(_events(
        ("text", "how many "), ("text", "hours until"), ("text", " halloween?"), ("done", None),
    ))
    assert _out(console) == "how many hours until halloween?\n"


def test_whole_lines_never_leaves_a_partial_line_on_the_console():
    """Every write that reaches the file must end at a line boundary."""
    console = _console()
    writes = []
    real_write = console.file.write
    console.file.write = lambda s: (writes.append(s), real_write(s))[1]
    Renderer(console, whole_lines=True).consume(_events(
        ("think", "hmm "), ("think", "ok"), ("text", "first\nsec"), ("text", "ond"),
        ("tool_start", {"name": "read_file", "args": {"path": "a.txt"}}),
        ("tool_result", {"result": "x\ny"}), ("text", "done"), ("done", None),
    ))
    assert _out(console).endswith("\n")
    # rich flushes each console.print as one write; none may be a dangling fragment.
    for w in writes:
        assert w == "" or w.endswith("\n"), repr(w)


def test_whole_lines_flushes_pending_text_before_a_tool_line():
    console = _console()
    Renderer(console, whole_lines=True).consume(_events(
        ("text", "Let me check"),
        ("tool_start", {"name": "read_file", "args": {"path": "a.txt"}}),
        ("tool_result", {"result": "contents"}),
        ("text", "Done."), ("done", None),
    ))
    lines = _out(console).splitlines()
    assert lines[0] == "Let me check"
    assert "read_file a.txt" in lines[1]
    assert lines[-1] == "Done."


def test_token_mode_still_streams_tokens():
    console = _console()
    out = Renderer(console, whole_lines=False).consume(_events(("text", "a"), ("text", "b"), ("done", None)))
    assert out == "ab"
    assert "ab" in _out(console)


# --- Renderer: model text is not markup -----------------------------------

@pytest.mark.parametrize("whole", [False, True])
def test_windows_path_in_thinking_survives(whole):
    """The bug from a real transcript: C:\\Users\\jsettle became C:[/dim]Users[/dim]jsettle."""
    console = _console()
    Renderer(console, whole_lines=whole).consume(_events(
        ("think", "C:\\"), ("think", "Users\\"), ("think", "jsettle"), ("done", None),
    ))
    assert "C:\\Users\\jsettle" in _out(console)
    assert "[/dim]" not in _out(console)


@pytest.mark.parametrize("whole", [False, True])
def test_brackets_in_answer_print_literally(whole):
    console = _console()
    Renderer(console, whole_lines=whole).consume(_events(("text", "use [bold] and [/x] here"), ("done", None)))
    assert "use [bold] and [/x] here" in _out(console)


def test_tool_result_with_brackets_does_not_break_markup():
    console = _console()
    Renderer(console).consume(_events(
        ("tool_start", {"name": "list_directory", "args": {"path": "[weird] dir"}}),
        ("tool_result", {"result": "[DIR]  .gemma/\n[FILE] a.txt"}),
        ("done", None),
    ))
    text = _out(console)
    assert "[weird] dir" in text
    assert "[DIR]  .gemma/" in text


# --- StatusBar ------------------------------------------------------------

def _cfg(**over):
    base = {"model": "gemma4:12b", "status_line": True, "status_refresh": 0.05,
            "status_segments": ["folder", "gpu", "vram", "cpu", "model"]}
    base.update(over)
    return base


def test_statusbar_is_a_noop_without_a_terminal(monkeypatch):
    monkeypatch.setattr(statusline, "_stdout_is_tty", lambda: True)
    with StatusBar(_console(terminal=False), _cfg()) as bar:
        assert bar.enabled is False
        assert bar._live is None


def test_statusbar_is_a_noop_when_stdout_is_redirected(monkeypatch):
    """Found live: main.py forces a 'terminal' console whenever stdin is a tty,
    so `gemma -p ... > file` would have written every bar frame into the file."""
    monkeypatch.setattr(statusline, "_stdout_is_tty", lambda: False)
    with StatusBar(_console(terminal=True), _cfg()) as bar:
        assert bar.enabled is False
        assert bar._live is None


def test_statusbar_is_a_noop_when_disabled_in_config(monkeypatch):
    monkeypatch.setattr(statusline, "_stdout_is_tty", lambda: True)
    with StatusBar(_console(terminal=True), _cfg(status_line=False)) as bar:
        assert bar.enabled is False
        assert bar._live is None


def test_statusbar_runs_and_stops_cleanly_on_a_terminal(monkeypatch):
    monkeypatch.setattr(statusline, "_stdout_is_tty", lambda: True)
    monkeypatch.setattr(statusline, "sample",
                        lambda: Sample(gpu_pct=42, vram_used=6144, vram_total=8192, cpu_pct=33.0))
    console = _console(terminal=True)
    bar = StatusBar(console, _cfg())
    with bar:
        assert bar.enabled
        assert bar._live is not None
        assert bar._thread is not None and bar._thread.is_alive()
        # Printing whole lines above the bar must not raise.
        Renderer(console, whole_lines=True).consume(_events(("text", "hello\n"), ("done", None)))
    assert bar._live is None
    assert bar._thread is None
    assert "hello" in _out(console)


def test_statusbar_renders_the_same_segments_as_live_mode(monkeypatch):
    monkeypatch.setattr(statusline, "sample",
                        lambda: Sample(gpu_pct=42, vram_used=6144, vram_total=8192, cpu_pct=33.0))
    bar = StatusBar(_console(terminal=True), _cfg())
    bar._sample = statusline.sample()
    text = bar._renderable().plain
    assert "GPU 42%" in text and "VRAM 6.0/8.0G" in text and "CPU 33%" in text and "gemma4:12b" in text


# --- _run_once wiring -----------------------------------------------------

class _FakeBar:
    made = []

    def __init__(self, console, cfg):
        self.enabled = True
        self.entered = False
        _FakeBar.made.append(self)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *a):
        return False


def _turn(cfg, messages, text, image_paths=None, approver=None, cancel=None):
    messages.append({"role": "user", "content": text})
    yield ("text", "hi\n")
    messages.append({"role": "assistant", "content": "hi"})
    yield ("done", None)


def test_run_once_uses_the_bar_and_whole_lines(monkeypatch):
    monkeypatch.setattr(main_mod, "run_turn", _turn)
    monkeypatch.setattr(statusline, "StatusBar", _FakeBar)
    _FakeBar.made.clear()
    console = _console()
    renderer = Renderer(console)
    seen = {}
    real_consume = renderer.consume
    renderer.consume = lambda ev: seen.setdefault("whole", renderer.whole_lines) or real_consume(ev)

    main_mod._run_once(_cfg(), [{"role": "system", "content": "s"}], console, renderer, "hello")

    assert len(_FakeBar.made) == 1 and _FakeBar.made[0].entered
    assert seen["whole"] is True          # whole-line mode while the bar was up
    assert renderer.whole_lines is False  # restored afterwards


def test_run_once_skips_the_bar_when_an_approver_is_set(monkeypatch):
    monkeypatch.setattr(main_mod, "run_turn", _turn)
    monkeypatch.setattr(statusline, "StatusBar", _FakeBar)
    _FakeBar.made.clear()
    console = _console()
    main_mod._run_once(_cfg(), [{"role": "system", "content": "s"}], console, Renderer(console), "hello",
                       approver=lambda n, a: True)
    assert _FakeBar.made == []
