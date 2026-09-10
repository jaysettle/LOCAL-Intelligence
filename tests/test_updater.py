"""Tests for `gemma update`.

Repo detection, version comparison and the install-command choice are tested
against real directories. The git calls and the actual pip install are the two
things stubbed — a test must not reach the network or reinstall the package it
is running from.
"""
import subprocess
import sys
from pathlib import Path

import pytest

from gemma_cli import updater


def _make_repo(path: Path, version="1.2.3", name="local-intelligence"):
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(updater, "remember_repo", lambda p: None)
    # Neutralise the editable-install candidate: without this, a test run from
    # inside a real checkout would always find that one first (by design).
    monkeypatch.setattr(updater, "_package_repo", lambda: tmp_path / "not-a-repo")
    return tmp_path


# --- recognising a checkout ----------------------------------------------

def test_is_repo_accepts_a_real_checkout(env):
    assert updater.is_repo(_make_repo(env / "repo"))


def test_is_repo_rejects_plain_directory(env):
    (env / "nope").mkdir()
    assert not updater.is_repo(env / "nope")


def test_is_repo_rejects_a_different_project(env):
    other = _make_repo(env / "other", name="some-other-package")
    assert not updater.is_repo(other)


def test_is_repo_rejects_checkout_without_git(env):
    path = env / "nogit"
    path.mkdir()
    (path / "pyproject.toml").write_text('name = "local-intelligence"', encoding="utf-8")
    assert not updater.is_repo(path)


def test_is_repo_handles_none():
    assert not updater.is_repo(None)


# --- finding it -----------------------------------------------------------

def test_explicit_repo_wins(env):
    explicit = _make_repo(env / "explicit")
    remembered = _make_repo(env / "remembered")
    found = updater.find_repo({"repo_dir": str(remembered)}, str(explicit))
    assert found == explicit.resolve()


def test_falls_back_to_config(env):
    remembered = _make_repo(env / "remembered")
    assert updater.find_repo({"repo_dir": str(remembered)}) == remembered.resolve()


def test_finds_repo_from_cwd(env, monkeypatch):
    repo = _make_repo(env / "checkout")
    monkeypatch.chdir(repo)
    assert updater.find_repo({}) == repo.resolve()


def test_finds_repo_from_a_subdirectory(env, monkeypatch):
    repo = _make_repo(env / "checkout")
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert updater.find_repo({}) == repo.resolve()


def test_bad_config_path_is_skipped(env, monkeypatch):
    repo = _make_repo(env / "checkout")
    monkeypatch.chdir(repo)
    assert updater.find_repo({"repo_dir": str(env / "does-not-exist")}) == repo.resolve()


# --- versions -------------------------------------------------------------

def test_repo_version(env):
    assert updater.repo_version(_make_repo(env / "r", version="0.4.0")) == "0.4.0"


def test_repo_version_unknown_when_missing(env):
    (env / "empty").mkdir()
    assert updater.repo_version(env / "empty") == "?"


@pytest.mark.parametrize("candidate,current,expected", [
    ("0.4.0", "0.3.0", True),
    ("0.10.0", "0.9.0", True),
    ("1.0.0", "1.0.0", False),
    ("0.3.0", "0.4.0", False),
    ("?", "0.3.0", False),
    ("0.4.0", "?", False),
])
def test_is_newer(candidate, current, expected):
    assert updater.is_newer(candidate, current) is expected


# --- install command ------------------------------------------------------

def test_default_install_uses_this_interpreter(env):
    repo = _make_repo(env / "r")
    command = updater.install_command(repo, full=False)
    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert str(repo) in command


def test_full_install_runs_the_platform_installer(env, monkeypatch):
    repo = _make_repo(env / "r")

    monkeypatch.setattr(updater, "_IS_WINDOWS", True)
    windows = updater.install_command(repo, full=True)
    assert "install.ps1" in " ".join(windows)
    assert "-SkipUpdate" in windows      # the installer must not re-pull

    monkeypatch.setattr(updater, "_IS_WINDOWS", False)
    posix = updater.install_command(repo, full=True)
    assert "install.sh" in " ".join(posix)
    assert "--skip-update" in posix


# --- git pull -------------------------------------------------------------

def _stub_git(monkeypatch, results):
    """Replace updater._git with a scripted sequence keyed by first argument."""
    def fake(repo, *args, **kwargs):
        key = args[0]
        value = results.get(key, ("", "", 0))
        stdout, stderr, code = value if isinstance(value, tuple) else (value, "", 0)
        if callable(stdout):
            stdout = stdout()
        return subprocess.CompletedProcess(args, code, stdout, stderr)
    monkeypatch.setattr(updater, "_git", fake)


def test_pull_reports_already_up_to_date(env, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda n: "git")
    _stub_git(monkeypatch, {"rev-parse": "abc123", "pull": "Already up to date."})
    ok, message = updater.git_pull(env)
    assert ok and message == "already up to date"


def test_pull_reports_change(env, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda n: "git")
    shas = iter(["old", "new"])
    _stub_git(monkeypatch, {"rev-parse": lambda: next(shas), "pull": "Fast-forward"})
    ok, message = updater.git_pull(env)
    assert ok and "Fast-forward" in message


def test_pull_failure_is_reported(env, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda n: "git")
    _stub_git(monkeypatch, {"rev-parse": "abc", "pull": ("", "diverged", 1)})
    ok, message = updater.git_pull(env)
    assert not ok and "diverged" in message


def test_stderr_alone_is_not_failure(env, monkeypatch):
    """git writes progress to stderr; only the return code decides."""
    monkeypatch.setattr(updater.shutil, "which", lambda n: "git")
    shas = iter(["old", "new"])
    _stub_git(monkeypatch, {
        "rev-parse": lambda: next(shas),
        "pull": ("", "Receiving objects: 100%", 0),
    })
    ok, _ = updater.git_pull(env)
    assert ok


def test_pull_without_git_installed(env, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda n: None)
    ok, message = updater.git_pull(env)
    assert not ok and "git is not installed" in message


# --- the deferred Windows path -------------------------------------------

def test_deferred_script_waits_for_this_process(env, monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "config_dir", lambda: tmp_path, raising=False)
    from gemma_cli import config
    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)

    script = updater._write_deferred_script(["echo", "hi"], tmp_path / "log.txt")
    text = script.read_text(encoding="utf-8")
    assert "GetExitCodeProcess" in text          # stdlib ctypes, not psutil
    assert "psutil" not in text                  # psutil can fail to import at runtime
    assert "WinError 32" in text          # explains the lock if it still happens
    assert "['echo', 'hi']" in text


def test_launcher_lock_detection_off_windows(monkeypatch):
    monkeypatch.setattr(updater, "_IS_WINDOWS", False)
    assert updater._launcher_is_locked() is False


def test_launcher_not_locked_when_no_shim(monkeypatch):
    monkeypatch.setattr(updater, "launcher_path", lambda: None)
    assert updater._launcher_is_locked() is False


def test_launcher_locked_when_write_is_refused(tmp_path, monkeypatch):
    """A running .exe refuses a write handle; that is the real signal."""
    exe = tmp_path / "gemma.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(updater, "launcher_path", lambda: exe)
    assert updater._launcher_is_locked() is False   # writable -> not running

    real_open = updater.open if hasattr(updater, "open") else open

    def refuse(path, mode="r", *a, **k):
        if str(path) == str(exe):
            raise PermissionError(32, "being used by another process")
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", refuse)
    assert updater._launcher_is_locked() is True


def test_reinstall_runs_inline_when_not_locked(env, monkeypatch, tmp_path):
    from rich.console import Console
    from gemma_cli import config

    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_launcher_is_locked", lambda: False)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(updater.subprocess, "run", fake_run)
    assert updater.reinstall(_make_repo(env / "r"), Console(no_color=True)) == 0
    assert calls and "pip" in calls[0]


def test_reinstall_reports_failure(env, monkeypatch, tmp_path):
    from rich.console import Console
    from gemma_cli import config

    monkeypatch.setattr(config, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(updater, "_launcher_is_locked", lambda: False)
    monkeypatch.setattr(
        updater.subprocess, "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "boom"),
    )
    assert updater.reinstall(_make_repo(env / "r"), Console(no_color=True)) == 1


# --- end to end (with git and pip stubbed) -------------------------------

def test_update_pulls_then_installs(env, monkeypatch):
    from rich.console import Console

    repo = _make_repo(env / "r", version="9.9.9")
    monkeypatch.setattr(updater, "git_pull", lambda r: (True, "Fast-forward"))
    monkeypatch.setattr(updater, "has_local_changes", lambda r: False)
    monkeypatch.setattr(updater, "current_branch", lambda r: "dev")
    installed = []
    monkeypatch.setattr(updater, "reinstall", lambda r, c, full=False: installed.append((r, full)) or 0)

    assert updater.update({"repo_dir": str(repo)}, Console(no_color=True)) == 0
    assert installed == [(repo.resolve(), False)]


def test_update_stops_when_pull_fails(env, monkeypatch):
    from rich.console import Console

    repo = _make_repo(env / "r")
    monkeypatch.setattr(updater, "git_pull", lambda r: (False, "diverged"))
    monkeypatch.setattr(updater, "has_local_changes", lambda r: False)
    monkeypatch.setattr(updater, "current_branch", lambda r: "dev")
    monkeypatch.setattr(updater, "reinstall", lambda *a, **k: pytest.fail("must not install"))

    assert updater.update({"repo_dir": str(repo)}, Console(no_color=True)) == 1


def test_update_skips_install_when_already_current(env, monkeypatch):
    from rich.console import Console
    from gemma_cli import __version__

    repo = _make_repo(env / "r", version=__version__)
    monkeypatch.setattr(updater, "git_pull", lambda r: (True, "already up to date"))
    monkeypatch.setattr(updater, "has_local_changes", lambda r: False)
    monkeypatch.setattr(updater, "current_branch", lambda r: "dev")
    monkeypatch.setattr(updater, "reinstall", lambda *a, **k: pytest.fail("must not install"))

    assert updater.update({"repo_dir": str(repo)}, Console(no_color=True)) == 0


def test_check_only_never_installs(env, monkeypatch):
    from rich.console import Console

    repo = _make_repo(env / "r")
    _stub_git(monkeypatch, {"fetch": "", "rev-list": "3", "rev-parse": "abc", "status": ""})
    monkeypatch.setattr(updater, "git_pull", lambda r: pytest.fail("must not pull"))
    monkeypatch.setattr(updater, "reinstall", lambda *a, **k: pytest.fail("must not install"))

    assert updater.update({"repo_dir": str(repo)}, Console(no_color=True), check_only=True) == 0


def test_update_without_a_repo_asks_before_cloning(env, monkeypatch):
    """With no checkout and no confirmation, nothing is cloned or installed."""
    from rich.console import Console

    monkeypatch.setattr(updater, "find_repo", lambda cfg, arg=None: None)
    monkeypatch.setattr(updater.shutil, "which", lambda n: "git")
    monkeypatch.setattr(updater.subprocess, "run", lambda *a, **k: pytest.fail("must not clone"))
    monkeypatch.setattr(Console, "input", lambda self, *a, **k: "n")

    assert updater.update({}, Console(no_color=True)) == 1


# --- CLI wiring -----------------------------------------------------------

def test_update_is_reachable_from_the_cli(monkeypatch):
    from gemma_cli import main as main_mod

    seen = {}

    def fake_update(cfg, console, repo_arg=None, check_only=False, full=False):
        seen.update(repo_arg=repo_arg, check_only=check_only, full=full)
        return 0

    monkeypatch.setattr("gemma_cli.updater.update", fake_update)
    assert main_mod.main(["update", "--check"]) == 0
    assert seen == {"repo_arg": None, "check_only": True, "full": False}


def test_update_accepts_a_path_argument(monkeypatch):
    from gemma_cli import main as main_mod

    seen = {}
    monkeypatch.setattr(
        "gemma_cli.updater.update",
        lambda cfg, console, repo_arg=None, check_only=False, full=False:
            seen.update(repo_arg=repo_arg) or 0,
    )
    assert main_mod.main(["update", "C:/src/LOCAL-Intelligence"]) == 0
    assert seen["repo_arg"] == "C:/src/LOCAL-Intelligence"


def test_update_is_not_treated_as_a_prompt(monkeypatch):
    """`gemma update` must never fall through to the one-shot prompt path."""
    from gemma_cli import main as main_mod

    monkeypatch.setattr("gemma_cli.updater.update", lambda *a, **k: 0)
    monkeypatch.setattr(main_mod, "_run_once", lambda *a, **k: pytest.fail("ran as a prompt"))
    assert main_mod.main(["update"]) == 0
