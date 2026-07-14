#!/usr/bin/env python3
"""
File tools: read_file, write_file, edit_file, delete_file, glob, list_directory.
Mutating tools are fenced to the configured allowed write roots; overwrites and
edits back up the prior version to .gemma/backups first; deletes go to the OS
trash (recoverable).
"""

import glob as glob_module
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# Populated by config.apply_to_tools() at startup.
ALLOWED_WRITE_ROOTS: List[Path] = []


def set_allowed_write_roots(roots: List[Path]) -> None:
    global ALLOWED_WRITE_ROOTS
    ALLOWED_WRITE_ROOTS = [Path(r).expanduser().resolve() for r in roots]


def _is_write_allowed(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except Exception:
        return False
    for root in ALLOWED_WRITE_ROOTS:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _write_denied_msg(target: Path) -> str:
    roots = ", ".join(str(r) for r in ALLOWED_WRITE_ROOTS) or "(none configured)"
    return f"Error: writing outside the allowed roots is not permitted: {target}\nAllowed roots: {roots}"


def _backup_existing(target: Path) -> None:
    """Best-effort copy of an existing file into .gemma/backups before overwrite."""
    try:
        if not target.exists() or not target.is_file():
            return
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path(".gemma") / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_dir / f"{target.name}.{ts}.bak")
    except Exception:
        pass  # never let a backup failure block the actual operation


def read_file(inp: Dict[str, Any]) -> str:
    path = os.path.expanduser(str(inp.get("path", "")))
    offset = int(inp.get("offset", 1) or 1)
    limit = int(inp.get("limit", 500) or 500)

    if not path:
        return "Error: 'path' is required"
    if not os.path.exists(path):
        return f"Error: File not found: {path}"
    if os.path.isdir(path):
        return f"Error: Path is a directory, not a file: {path}"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"Error reading file: {e}"

    start = max(0, offset - 1)
    end = start + limit
    selected = lines[start:end]
    out = [f"{i:6d}\t{line.rstrip()}" for i, line in enumerate(selected, start=start + 1)]
    result = "\n".join(out)
    if len(lines) > end:
        result += f"\n... ({len(lines) - end} more lines)"
    return result or "(empty file)"


def write_file(inp: Dict[str, Any]) -> str:
    raw = str(inp.get("path", ""))
    content = inp.get("content", "")
    if not raw:
        return "Error: 'path' is required"

    target = Path(os.path.expanduser(raw))
    if not _is_write_allowed(target):
        return _write_denied_msg(target)

    try:
        _backup_existing(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {target}"
    except Exception as e:
        return f"Error writing file: {e}"


def edit_file(inp: Dict[str, Any]) -> str:
    """Replace an exact substring in a file. Preferred over write_file for edits."""
    raw = str(inp.get("path", ""))
    old = inp.get("old_string")
    new = inp.get("new_string", "")
    replace_all = bool(inp.get("replace_all", False))

    if not raw:
        return "Error: 'path' is required"
    if old is None or old == "":
        return "Error: 'old_string' is required (the exact text to replace)"
    if old == new:
        return "Error: old_string and new_string are identical; nothing to change."

    target = Path(os.path.expanduser(raw))
    if not _is_write_allowed(target):
        return _write_denied_msg(target)
    if not target.exists():
        return f"Error: File not found: {target}"
    if target.is_dir():
        return f"Error: Path is a directory, not a file: {target}"

    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"

    count = text.count(old)
    if count == 0:
        return (
            f"Error: old_string not found in {target}. Read the file first and copy the "
            "exact text to replace, including whitespace and indentation."
        )
    if count > 1 and not replace_all:
        return (
            f"Error: old_string appears {count} times in {target}; it must match exactly once. "
            "Include more surrounding context to make it unique, or pass replace_all=true."
        )

    try:
        _backup_existing(target)
        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        target.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return f"Error writing file: {e}"

    n = count if replace_all else 1
    return f"Successfully replaced {n} occurrence(s) in {target}"


def delete_file(inp: Dict[str, Any]) -> str:
    """Delete a file or folder by sending it to the OS trash (recoverable)."""
    raw = str(inp.get("path", ""))
    if not raw:
        return "Error: 'path' is required"

    target = Path(os.path.expanduser(raw))
    if not _is_write_allowed(target):
        return _write_denied_msg(target)
    if not target.exists():
        return f"Error: path not found: {target}"

    # Preferred: OS recycle bin / trash via send2trash.
    try:
        from send2trash import send2trash
        send2trash(str(target.resolve()))
        return f"Moved to trash/recycle bin (recoverable): {target}"
    except ImportError:
        pass  # fall back to an in-project trash folder
    except Exception as e:
        return f"Error moving to trash: {e}"

    try:
        trash_dir = Path(".gemma") / "trash"
        trash_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(str(target), str(trash_dir / f"{target.name}.{ts}"))
        return f"Moved to {trash_dir} (recoverable; send2trash not installed): {target}"
    except Exception as e:
        return f"Error deleting: {e}"


def glob_files(inp: Dict[str, Any]) -> str:
    pattern = str(inp.get("pattern", ""))
    base = os.path.expanduser(str(inp.get("path", ".")))
    if not pattern:
        return "Error: 'pattern' is required"

    try:
        full = os.path.join(base, pattern)
        matches = glob_module.glob(full, recursive=True)
        matches.sort(key=lambda x: os.path.getmtime(x) if os.path.exists(x) else 0, reverse=True)
    except Exception as e:
        return f"Error in glob: {e}"

    if not matches:
        return "No files found matching pattern"
    if len(matches) > 100:
        shown = matches[:100]
        return "\n".join(shown) + f"\n... ({len(matches)} total, first 100 shown)"
    return "\n".join(matches)


def list_directory(inp: Dict[str, Any]) -> str:
    path = os.path.expanduser(str(inp.get("path", ".")))
    if not os.path.exists(path):
        return f"Error: Path not found: {path}"
    if not os.path.isdir(path):
        return f"Error: Not a directory: {path}"

    try:
        entries = sorted(os.listdir(path))
    except Exception as e:
        return f"Error listing directory: {e}"

    out = []
    for entry in entries:
        full = os.path.join(path, entry)
        if os.path.isdir(full):
            out.append(f"[DIR]  {entry}/")
        else:
            try:
                out.append(f"[FILE] {entry} ({os.path.getsize(full)} bytes)")
            except OSError:
                out.append(f"[FILE] {entry}")
    return "\n".join(out) if out else "(empty directory)"
