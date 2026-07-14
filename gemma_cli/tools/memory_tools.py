#!/usr/bin/env python3
"""
Memory tool: `remember` appends a durable fact to project or global memory.

- project memory: GEMMA.md in the current working directory (committed with the
  project, auto-loaded into the system prompt by sysprompt.py)
- global memory: <config_dir>/memory.md (facts about the user/machine that apply
  everywhere)

Reading these files back into context happens in sysprompt.build_system_prompt().
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

# Set by config.apply_to_tools() at startup.
PROJECT_MEMORY_FILE = "GEMMA.md"
GLOBAL_MEMORY_FILE = ""


def configure(project_memory_file: str, global_memory_file: str) -> None:
    global PROJECT_MEMORY_FILE, GLOBAL_MEMORY_FILE
    PROJECT_MEMORY_FILE = project_memory_file or "GEMMA.md"
    GLOBAL_MEMORY_FILE = global_memory_file or ""


def _memory_path(scope: str) -> Path:
    if scope == "global":
        return Path(GLOBAL_MEMORY_FILE).expanduser() if GLOBAL_MEMORY_FILE else Path.home() / ".gemma_memory.md"
    # project scope: relative to the current working directory
    return Path(os.getcwd()) / PROJECT_MEMORY_FILE


def read_memory(scope: str) -> str:
    """Return the contents of a memory file, or '' if absent. Used by sysprompt."""
    try:
        p = _memory_path(scope)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return ""


def remember(inp: Dict[str, Any]) -> str:
    """Append a fact to project (default) or global memory."""
    text = str(inp.get("text", "")).strip()
    scope = str(inp.get("scope", "project")).lower()
    if scope not in ("project", "global"):
        scope = "project"
    if not text:
        return "Error: 'text' is required (the fact to remember)"

    path = _memory_path(scope)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        header_needed = not path.exists()
        ts = datetime.now().strftime("%Y-%m-%d")
        with open(path, "a", encoding="utf-8") as f:
            if header_needed:
                title = "Project Memory" if scope == "project" else "Global Memory"
                f.write(f"# {title}\n\nDurable notes remembered by the local agent.\n\n")
            f.write(f"- ({ts}) {text}\n")
        return f"Remembered ({scope}) in {path}: {text}"
    except Exception as e:
        return f"Error writing memory: {e}"
