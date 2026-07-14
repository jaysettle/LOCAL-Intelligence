#!/usr/bin/env python3
"""
Shell tools: shell (PowerShell on Windows / bash on POSIX) and a pure-Python grep.
Dangerous-command blocklists are per-OS. This is a guard-rail, not a sandbox — the
agent runs with the user's own privileges by design (that's the point of the tool).
"""

import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any, Dict

_IS_WINDOWS = platform.system() == "Windows"

# Regex patterns (case-insensitive) that abort a shell command outright.
# Anchored/specific to avoid false positives on legitimate commands like
# `Get-Date -Format ...` or `ConvertTo-Json`.
_DANGEROUS_WINDOWS = [
    r"\bformat\s+[a-z]:",              # format C:
    r"\breg\s+delete\s+hk(lm|ey_local_machine)\b",
    r"\bvssadmin\s+delete\b",          # delete shadow copies
    r"\bcipher\s+/w",                  # wipe free space
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\bremove-item\b.*-recurse.*\b[a-z]:\\?\s*$",   # recursive delete of a drive root
    r"\brd\s+/s\s+/q\s+[a-z]:",
    r"\bdel\s+/[fsq/ ]*\s+[a-z]:\\?\s*$",
    r"\b(shutdown|stop-computer|restart-computer)\b",
]
_DANGEROUS_POSIX = [
    r"\brm\s+-rf\s+/",
    r"\brm\s+-rf\s+~",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r":\(\)\{\s*:\|:&\s*\};:",
    r"\b(shutdown|reboot)\b",
    r"\bsudo\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in (_DANGEROUS_WINDOWS if _IS_WINDOWS else _DANGEROUS_POSIX)]


def _blocked(command: str) -> bool:
    return any(rx.search(command) for rx in _COMPILED)


def shell(inp: Dict[str, Any]) -> str:
    command = str(inp.get("command", ""))
    timeout = int(inp.get("timeout", 60) or 60)
    if not command.strip():
        return "Error: 'command' is required"
    if _blocked(command):
        return f"Error: potentially destructive command blocked: {command}"

    if _IS_WINDOWS:
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["bash", "-lc", command]

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.path.expanduser("~"),
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout} seconds"
    except Exception as e:
        return f"Error executing command: {e}"

    out = result.stdout or ""
    if result.stderr:
        out += ("\n--- stderr ---\n" if out else "") + result.stderr
    if result.returncode != 0:
        out += f"\n[Exit code: {result.returncode}]"
    if len(out) > 50000:
        out = out[:50000] + "\n... (output truncated)"
    return out if out.strip() else "(no output)"


def grep(inp: Dict[str, Any]) -> str:
    """Pure-Python recursive regex search (portable; Windows has no grep)."""
    pattern = str(inp.get("pattern", ""))
    root = os.path.expanduser(str(inp.get("path", ".")))
    file_pattern = str(inp.get("file_pattern", "*") or "*")
    if not pattern:
        return "Error: 'pattern' is required"

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"

    from fnmatch import fnmatch

    matches = []
    max_matches = 100
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}

    def scan_file(fpath: str):
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        matches.append(f"{fpath}:{lineno}:{line.rstrip()[:300]}")
                        if len(matches) >= max_matches:
                            return
        except (OSError, UnicodeError):
            return

    if os.path.isfile(root):
        scan_file(root)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                if fnmatch(fn, file_pattern):
                    scan_file(os.path.join(dirpath, fn))
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break

    if not matches:
        return f"No matches found for pattern '{pattern}'"
    result = "\n".join(matches)
    if len(matches) >= max_matches:
        result += f"\n... (stopped at {max_matches} matches)"
    return result
