#!/usr/bin/env python3
"""
`gemma update` — pull the latest source and reinstall, from a normal terminal.

The awkward part is Windows. A pip reinstall has to overwrite `gemma.exe`, and
Windows locks a running executable's image file, so an install launched from
inside a running `gemma` fails with WinError 32 — and can leave a corrupt
`~ocal_intelligence*.dist-info` marker that jams every later install. So the git
pull happens here (safe, touches no installed files) and the *install* is handed
to a detached helper that waits for this process to exit first. On POSIX the
launcher is a plain script, nothing is locked, and the install runs inline.

Finding the source: an explicit --repo, then the path remembered in config, then
the package's own location (editable installs), then the working directory and
its parents. Failing all that, and only with the user's say-so, a fresh clone
into the config directory — so the ZIP-download crowd gets working updates too.
"""

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_URL = "https://github.com/jaysettle/LOCAL-Intelligence.git"

_IS_WINDOWS = platform.system() == "Windows"
_VERSION_RE = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


# ---------------------------------------------------------------------------
# Locating the source checkout
# ---------------------------------------------------------------------------

def is_repo(path: Optional[Path]) -> bool:
    """True if `path` looks like a LOCAL-Intelligence checkout."""
    if not path:
        return False
    try:
        pyproject = path / "pyproject.toml"
        if not (path / ".git").exists() or not pyproject.is_file():
            return False
        return "local-intelligence" in pyproject.read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        return False


def find_repo(cfg: Dict[str, Any], explicit: Optional[str] = None) -> Optional[Path]:
    for candidate in _repo_candidates(cfg, explicit):
        if is_repo(candidate):
            return candidate.resolve()
    return None


def _package_repo() -> Path:
    """Where this package is installed from — the checkout, for editable installs.

    For a normal install this is inside site-packages and `is_repo` rejects it.
    """
    return Path(__file__).resolve().parent.parent


def _repo_candidates(cfg: Dict[str, Any], explicit: Optional[str]):
    """Candidate checkouts, best first.

    Priority matters: the checkout `gemma` actually runs from beats whatever
    folder the user happens to be standing in, since updating the latter would
    leave the running install untouched.
    """
    if explicit:
        yield Path(os.path.expanduser(explicit))
    remembered = cfg.get("repo_dir")
    if remembered:
        yield Path(os.path.expanduser(str(remembered)))
    yield _package_repo()
    cwd = Path.cwd()
    yield cwd
    yield from cwd.parents
    yield Path.home() / "LOCAL-Intelligence"


def remember_repo(path: Path) -> None:
    """Persist the checkout location into config.yaml so the next run is instant."""
    try:
        import yaml
        from .config import CONFIG_PATH

        existing: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            existing = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        if str(existing.get("repo_dir") or "") == str(path):
            return
        existing["repo_dir"] = str(path)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            "# LOCAL-Intelligence (gemma) configuration\n"
            "# Edit values below; env vars (GEMMA_MODEL, GEMMA_NUM_CTX, ...) override these.\n\n"
            + yaml.safe_dump(existing, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
    except Exception:
        pass  # remembering is a convenience, never a failure


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def repo_version(repo: Path) -> str:
    try:
        match = _VERSION_RE.search((repo / "pyproject.toml").read_text(encoding="utf-8"))
        return match.group(1) if match else "?"
    except Exception:
        return "?"


def _as_tuple(version: str) -> Tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    if "?" in (candidate, current):
        return False
    return _as_tuple(candidate) > _as_tuple(current)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def current_branch(repo: Path) -> str:
    try:
        result = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        return (result.stdout or "").strip() or "?"
    except Exception:
        return "?"


def has_local_changes(repo: Path) -> bool:
    try:
        result = _git(repo, "status", "--porcelain")
        return bool((result.stdout or "").strip())
    except Exception:
        return False


def git_pull(repo: Path) -> Tuple[bool, str]:
    """Fast-forward the checkout. Returns (ok, message).

    Success is judged by the return code, never by whether stderr had output —
    git writes progress to stderr, which is what makes 'git pull failed' a false
    alarm in shells that treat any stderr as failure.
    """
    if not shutil.which("git"):
        return False, "git is not installed or not on PATH."
    try:
        before = _git(repo, "rev-parse", "HEAD").stdout.strip()
        result = _git(repo, "pull", "--ff-only")
        after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "git pull timed out."
    except Exception as e:
        return False, f"git pull could not run: {e}"

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, output or f"git pull exited with code {result.returncode}"
    if before and before == after:
        return True, "already up to date"
    return True, output or "updated"


# ---------------------------------------------------------------------------
# Reinstalling
# ---------------------------------------------------------------------------

def launcher_path() -> Optional[Path]:
    """The gemma.exe shim pip would have to overwrite, if there is one."""
    if not _IS_WINDOWS:
        return None
    exe = Path(sys.executable).parent / "gemma.exe"
    return exe if exe.exists() else None


def _launcher_is_locked() -> bool:
    """True when gemma.exe is currently running and pip could not replace it.

    Rather than inferring this from sys.argv[0] — whose shape depends on which
    console-script launcher pip generated — this asks Windows directly: opening a
    running executable's image for writing raises a sharing violation. Appending
    zero bytes changes nothing, so the probe is free.
    """
    exe = launcher_path()
    if exe is None:
        return False
    try:
        with open(exe, "ab"):
            return False
    except OSError:
        return True
    except Exception:
        return True  # if in doubt, take the safe (deferred) path


def install_command(repo: Path, full: bool) -> List[str]:
    if not full:
        return [sys.executable, "-m", "pip", "install", "--upgrade", str(repo)]
    if _IS_WINDOWS:
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(repo / "install.ps1"), "-SkipUpdate"]
    return ["bash", str(repo / "install.sh"), "--skip-update"]


def _write_deferred_script(command: List[str], log_path: Path) -> Path:
    """A helper that waits for this process to die, then runs the install."""
    from .config import config_dir

    script = config_dir() / "_deferred_update.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import subprocess, sys, time\n"
        f"PARENT = {os.getpid()}\n"
        f"COMMAND = {command!r}\n"
        f"LOG = {str(log_path)!r}\n"
        # Wait for the parent to exit so the lock on gemma.exe is gone. ctypes is
        # stdlib and cannot fail to import the way a compiled psutil can.
        "def alive(pid):\n"
        "    try:\n"
        "        import ctypes\n"
        "        k = ctypes.windll.kernel32\n"
        "        h = k.OpenProcess(0x1000, False, pid)\n"
        "        if not h:\n"
        "            return False\n"
        "        code = ctypes.c_ulong()\n"
        "        k.GetExitCodeProcess(h, ctypes.byref(code))\n"
        "        k.CloseHandle(h)\n"
        "        return code.value == 259\n"
        "    except Exception:\n"
        "        return None\n"
        "deadline = time.time() + 60\n"
        "while time.time() < deadline:\n"
        "    state = alive(PARENT)\n"
        "    if state is None:\n"
        "        time.sleep(3)\n"
        "        break\n"
        "    if not state:\n"
        "        break\n"
        "    time.sleep(0.25)\n"
        "time.sleep(0.5)\n"
        "print('Updating LOCAL-Intelligence...\\n')\n"
        "result = subprocess.run(COMMAND, capture_output=True, text=True)\n"
        "output = (result.stdout or '') + (result.stderr or '')\n"
        "try:\n"
        "    open(LOG, 'w', encoding='utf-8').write(output)\n"
        "except Exception:\n"
        "    pass\n"
        "print(output[-4000:])\n"
        "if result.returncode == 0:\n"
        "    print('\\nUpdate complete. Run: gemma --version')\n"
        "else:\n"
        "    print('\\nUpdate FAILED (exit %d).' % result.returncode)\n"
        "    if 'WinError 32' in output or 'being used by another process' in output:\n"
        "        print('A gemma session is still running and is holding gemma.exe open.')\n"
        "        print('Close every gemma window, then run: gemma update')\n"
        "try:\n"
        "    input('\\nPress Enter to close...')\n"
        "except Exception:\n"
        "    pass\n",
        encoding="utf-8",
    )
    return script


def reinstall(repo: Path, console, full: bool = False) -> int:
    """Reinstall the package. Returns a process exit code for main()."""
    from .config import config_dir

    command = install_command(repo, full)
    log_path = config_dir() / "last_update.log"

    if not _launcher_is_locked():
        console.print("[dim]reinstalling…[/dim]")
        result = subprocess.run(command, capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")
        try:
            log_path.write_text(output, encoding="utf-8")
        except Exception:
            pass
        if result.returncode != 0:
            console.print(f"[red]Install failed (exit {result.returncode}).[/red] [dim]{log_path}[/dim]")
            console.print(f"[dim]{output.strip()[-1500:]}[/dim]")
            return 1
        console.print("[green]Update complete.[/green] [dim]Run `gemma --version` to confirm.[/dim]")
        return 0

    # Windows, launched via gemma.exe: defer so the lock is gone first.
    script = _write_deferred_script(command, log_path)
    console.print(
        "[dim]Windows holds gemma.exe open while it runs, so the install is handed to a "
        "second window that starts once this one exits.[/dim]"
    )
    try:
        subprocess.Popen(
            [sys.executable, str(script)],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            close_fds=True,
        )
    except Exception as e:
        console.print(f"[red]Could not start the updater: {e}[/red]")
        console.print(f"[dim]Run it yourself:[/dim] {' '.join(command)}")
        return 1

    console.print("[green]Installer launched in a new window.[/green] [dim]This one is exiting so it can proceed.[/dim]")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def update(cfg: Dict[str, Any], console, repo_arg: Optional[str] = None,
           check_only: bool = False, full: bool = False) -> int:
    from . import __version__

    repo = find_repo(cfg, repo_arg)
    if repo is None:
        return _no_repo(cfg, console, check_only)

    remember_repo(repo)
    branch = current_branch(repo)
    console.print(f"[dim]source:[/dim] {repo} [dim](branch {branch})[/dim]")

    if has_local_changes(repo):
        console.print(
            "[yellow]That checkout has uncommitted changes.[/yellow] "
            "[dim]Pulling with --ff-only will not discard them, but the pull may refuse. "
            "Commit or stash them if it does.[/dim]"
        )

    if check_only:
        _git(repo, "fetch", "--quiet")
        result = _git(repo, "rev-list", "--count", "HEAD..@{u}")
        behind = (result.stdout or "0").strip() or "0"
        console.print(f"[dim]installed:[/dim] {__version__}   [dim]checkout:[/dim] {repo_version(repo)}")
        if behind.isdigit() and int(behind) > 0:
            console.print(f"[green]{behind} new commit(s) available.[/green] [dim]Run `gemma update` to install.[/dim]")
        else:
            console.print("[dim]No new commits on the tracked branch.[/dim]")
        return 0

    ok, message = git_pull(repo)
    console.print(f"[dim]git:[/dim] {message}")
    if not ok:
        console.print("[red]Could not pull the latest source.[/red]")
        return 1

    latest = repo_version(repo)
    if message == "already up to date" and not is_newer(latest, __version__) and latest == __version__:
        console.print(f"[green]Already on the latest version ({__version__}).[/green]")
        return 0

    console.print(f"[dim]installed {__version__} → checkout {latest}[/dim]")
    return reinstall(repo, console, full=full)


def _no_repo(cfg: Dict[str, Any], console, check_only: bool) -> int:
    from .config import config_dir

    console.print("[yellow]No LOCAL-Intelligence source checkout found.[/yellow]")
    if check_only:
        console.print("[dim]Point at one with: gemma update --check --repo <path>[/dim]")
        return 1

    target = config_dir() / "src"
    console.print(f"[dim]Looked in: the path in config, this package's folder, the current folder and its parents.[/dim]")
    if not shutil.which("git"):
        console.print("[dim]Install git, then re-run — or pass an existing clone with --repo <path>.[/dim]")
        return 1

    console.print(f"[dim]It can be cloned to {target} and updated from there from now on.[/dim]")
    try:
        answer = console.input("Clone it now? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return 1
    if answer not in ("y", "yes"):
        console.print("[dim]Nothing done. Use --repo <path> if you already have a clone.[/dim]")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "clone", REPO_URL, str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        console.print(f"[red]Clone failed:[/red] [dim]{(result.stderr or '').strip()[:500]}[/dim]")
        return 1

    remember_repo(target)
    console.print(f"[green]Cloned to {target}[/green]")
    return reinstall(target, console)
