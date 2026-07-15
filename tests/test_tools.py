"""Tests for the pure-Python tools (no Ollama/network needed)."""
import os
from pathlib import Path

import pytest

from gemma_cli.tools import file_tools, memory_tools
from gemma_cli.tools.shell_tools import _blocked
from gemma_cli.tools.web_tools import _TextExtractor


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    file_tools.set_allowed_write_roots([tmp_path])
    memory_tools.configure("GEMMA.md", str(tmp_path / "global_memory.md"))
    return tmp_path


# --- edit_file -----------------------------------------------------------

def test_edit_unique(project):
    f = project / "a.txt"
    f.write_text("one two three\n")
    out = file_tools.edit_file({"path": str(f), "old_string": "two", "new_string": "TWO"})
    assert "Successfully replaced 1" in out
    assert f.read_text() == "one TWO three\n"


def test_edit_ambiguous_requires_unique(project):
    f = project / "a.txt"
    f.write_text("x x x\n")
    out = file_tools.edit_file({"path": str(f), "old_string": "x", "new_string": "y"})
    assert "appears 3 times" in out
    assert f.read_text() == "x x x\n"  # unchanged


def test_edit_not_found(project):
    f = project / "a.txt"
    f.write_text("hello\n")
    out = file_tools.edit_file({"path": str(f), "old_string": "nope", "new_string": "y"})
    assert "not found" in out


def test_edit_replace_all(project):
    f = project / "a.txt"
    f.write_text("x x x\n")
    out = file_tools.edit_file({"path": str(f), "old_string": "x", "new_string": "y", "replace_all": True})
    assert "Successfully replaced 3" in out
    assert f.read_text() == "y y y\n"


def test_edit_identical_noop(project):
    f = project / "a.txt"
    f.write_text("hello\n")
    out = file_tools.edit_file({"path": str(f), "old_string": "hello", "new_string": "hello"})
    assert "identical" in out


# --- write_file guard + backup ------------------------------------------

def test_write_blocked_outside_roots(project):
    out = file_tools.write_file({"path": "/etc/evil", "content": "x"})
    assert "not permitted" in out


def test_write_backs_up_existing(project):
    f = project / "a.txt"
    f.write_text("original\n")
    file_tools.write_file({"path": str(f), "content": "new\n"})
    backups = list((project / ".gemma" / "backups").glob("a.txt.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text() == "original\n"
    assert f.read_text() == "new\n"


# --- delete_file ---------------------------------------------------------

def test_delete_removes_original(project):
    f = project / "gone.txt"
    f.write_text("bye\n")
    out = file_tools.delete_file({"path": str(f)})
    assert "recoverable" in out.lower() or "trash" in out.lower()
    assert not f.exists()


def test_delete_blocked_outside_roots(project):
    out = file_tools.delete_file({"path": "/etc/hosts"})
    assert "not permitted" in out


# --- shell blocklist -----------------------------------------------------

@pytest.mark.parametrize("cmd,blocked", [
    ("Get-Date -Format yyyy-MM-dd", False),
    ("Get-ChildItem | ConvertTo-Json", False),
    ("ls -la", False),
    ("format C: /y", True),
    ("reg delete HKLM\\Software\\Foo /f", True),
    ("shutdown /s /t 0", True),
    ("rm -rf /", True),
    ("sudo rm file", True),
])
def test_blocklist(cmd, blocked):
    assert _blocked(cmd) is blocked


# --- memory --------------------------------------------------------------

def test_remember_project_and_read(project):
    out = memory_tools.remember({"text": "Uses pytest.", "scope": "project"})
    assert "Remembered" in out
    assert (project / "GEMMA.md").exists()
    assert "Uses pytest." in memory_tools.read_memory("project")


def test_remember_requires_text(project):
    out = memory_tools.remember({"text": "  "})
    assert "required" in out


# --- web text extractor (offline) ---------------------------------------

def test_text_extractor_strips_html():
    ex = _TextExtractor()
    ex.feed("<html><head><style>x{}</style></head><body><p>Hello</p><script>bad()</script><p>World</p></body></html>")
    text = "".join(ex.parts)
    assert "Hello" in text and "World" in text
    assert "bad()" not in text and "x{}" not in text
