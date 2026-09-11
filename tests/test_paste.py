"""Pasted multi-line prompts in the plain REPL, the exit path, and comma globs.

Seen live: a multi-line prompt pasted into `gemma go` ran as one turn PER LINE.
The first line alone sent a thinking model into a minutes-long spiral about the
missing text; every other line then ran as its own turn; and a second Ctrl+C
during the goodbye print escaped as a traceback.
"""
import io
import sys

import pytest
from rich.console import Console

from gemma_cli import main as main_mod
from gemma_cli.tools import file_tools


class _Console:
    """Just enough of rich's Console for _read_prompt."""

    def __init__(self, first):
        self.first = first

    def input(self, prompt):
        return self.first


def test_pasted_block_becomes_one_prompt(monkeypatch):
    monkeypatch.setattr(main_mod, "_pending_input_lines", lambda: ["name: x", "mode: per-file", "---"])
    assert main_mod._read_prompt(_Console("Create a file with exactly this:")) == (
        "Create a file with exactly this:\nname: x\nmode: per-file\n---"
    )


def test_single_line_is_unchanged(monkeypatch):
    monkeypatch.setattr(main_mod, "_pending_input_lines", lambda: [])
    assert main_mod._read_prompt(_Console("  hello  ")) == "hello"


def test_commands_never_absorb_following_lines(monkeypatch):
    """/doc-index pasted above /check must stay two commands."""
    called = []
    monkeypatch.setattr(main_mod, "_pending_input_lines", lambda: called.append(1) or ["/check"])
    assert main_mod._read_prompt(_Console("/doc-index")) == "/doc-index"
    assert called == []          # the buffer was not even consulted


def test_pending_lines_are_never_read_from_a_pipe(monkeypatch):
    """Piped stdin keeps the line-by-line contract scripts and tests rely on."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("second\nthird\n"))
    assert main_mod._pending_input_lines() == []


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt is Windows-only")
def test_pending_lines_drain_the_windows_console_buffer(monkeypatch):
    import msvcrt

    class _TTY(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _TTY())
    queue = ["line two", "line three"]
    monkeypatch.setattr(msvcrt, "kbhit", lambda: bool(queue))
    monkeypatch.setattr("builtins.input", lambda *a: queue.pop(0))
    assert main_mod._pending_input_lines() == ["line two", "line three"]


def test_exit_survives_an_interrupted_goodbye(monkeypatch):
    """Ctrl+C at the prompt, then again while rich prints 'bye': exit 0, no traceback."""
    class _Boom(Console):
        def input(self, *a, **k):
            raise KeyboardInterrupt

        def print(self, *a, **k):
            # Only the goodbye is interrupted - that is the case seen live, where
            # a second Ctrl+C landed while rich was measuring the terminal.
            if a and "bye" in str(a[0]):
                raise KeyboardInterrupt
            return super().print(*a, **k)

    monkeypatch.setattr(main_mod, "_preflight", lambda cfg, console: None)
    monkeypatch.setattr(main_mod, "_banner", lambda cfg, console: None)
    code = main_mod._repl_plain({"model": "m"}, [{"role": "system", "content": ""}], _Boom(file=io.StringIO()),
                                None, None)
    assert code == 0


def test_glob_tool_accepts_comma_separated_patterns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for n in ("a.pdf", "b.docx", "c.txt"):
        (tmp_path / n).write_text("x")
    out = file_tools.glob_files({"pattern": "*.pdf, *.docx", "path": str(tmp_path)})
    assert "a.pdf" in out and "b.docx" in out and "c.txt" not in out


def test_glob_tool_single_pattern_still_works(tmp_path):
    (tmp_path / "only.pdf").write_text("x")
    out = file_tools.glob_files({"pattern": "*.pdf", "path": str(tmp_path)})
    assert "only.pdf" in out
