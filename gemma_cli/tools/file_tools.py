#!/usr/bin/env python3
"""
File tools: read_file, write_file, glob, list_directory.
write_file is fenced to the configured allowed write roots.
"""

import glob as glob_module
import os
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
        roots = ", ".join(str(r) for r in ALLOWED_WRITE_ROOTS) or "(none configured)"
        return f"Error: writing outside the allowed roots is not permitted: {target}\nAllowed roots: {roots}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote {len(content)} bytes to {target}"
    except Exception as e:
        return f"Error writing file: {e}"


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
