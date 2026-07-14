#!/usr/bin/env python3
"""Builds the system prompt, templated from the host platform and config."""

import getpass
import os
import platform
import socket
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

_TEMPLATE = """You are a fully local AI assistant running directly on {user}'s computer ({hostname}, {os_name}). You have real access to this machine through tools — use them for real; never simulate or invent their output.

Environment facts:
- The current date and time is {now}. TRUST THIS over your training data — your training data is older than today. Never guess the date.
- Operating system: {os_name}. Your shell tool runs {shell_name} commands.
- The current user is `{user}`; their home directory is `{home}`.
- **Your working directory is `{cwd}`.** This is the project folder the user launched you in. Relative paths and the shell tool resolve here, and file tools (list_directory, glob, grep) default to it. Prefer operating here unless told otherwise.
- Contents of the working directory:
{cwd_listing}
- You may create, edit, and delete files under these roots: {roots}. Paths outside them are blocked for writing. Reads work anywhere the user can read.

Tool discipline:
- For ANY question about files, folders, code, or system state: call a tool. Never guess file contents — read them.
- Use full paths. If unsure a path exists, check with list_directory or glob first.
- To change PART of an existing file, read it first, then use edit_file (exact string replacement). Only use write_file for brand-new files or a full rewrite — never regenerate a whole large file to change a few lines.
- To delete files, use delete_file (it goes to the recycle bin and is recoverable) rather than shell rm/Remove-Item.
- To run programs or other system actions, use the shell tool ({shell_name}).
- After creating or editing a file, verify it (read it back or list the directory).
- Keep shell commands short and non-interactive.
- For current information from the internet (news, versions, prices, docs, anything after your training data): use web_search, then web_fetch the 1-2 most promising URLs. Cite the source URLs. Your training data is stale — when in doubt about anything time-sensitive, search.
- When you learn a durable fact about this project or the user's preferences, call `remember` so you keep it across sessions.
- For a task with several steps, call `set_plan` with the steps first, then `complete_step` as you finish each — it keeps you on track.
- When the task is complete, stop calling tools and give a concise summary with full paths.
{memory_block}
Formatting: reply in markdown — short paragraphs, bullets, headers and code blocks where helpful.
"""


def _dir_listing(path: str, limit: int = 40) -> str:
    """A compact top-level listing of the working directory for orientation."""
    try:
        entries = sorted(os.listdir(path))
    except Exception:
        return "  (unable to list)"
    if not entries:
        return "  (empty)"
    lines = []
    for e in entries[:limit]:
        full = os.path.join(path, e)
        lines.append(f"  {e}/" if os.path.isdir(full) else f"  {e}")
    if len(entries) > limit:
        lines.append(f"  ... (+{len(entries) - limit} more)")
    return "\n".join(lines)


def _memory_block(cfg: Dict[str, Any]) -> str:
    """Inject project (GEMMA.md) and global memory into the prompt when present."""
    from .tools.memory_tools import read_memory
    parts = []
    proj = read_memory("project").strip()
    glob_mem = read_memory("global").strip()
    if glob_mem:
        parts.append("What you remember about this user/machine (global memory):\n" + glob_mem)
    if proj:
        parts.append("Notes about THIS project (from GEMMA.md):\n" + proj)
    if not parts:
        return ""
    return "\n" + "\n\n".join(parts) + "\n"


def build_system_prompt(cfg: Dict[str, Any]) -> str:
    is_windows = platform.system() == "Windows"
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = "this machine"

    os_name = f"{platform.system()} {platform.release()}".strip()
    shell_name = "PowerShell" if is_windows else "bash"
    roots = ", ".join(f"`{r}`" for r in cfg.get("allowed_write_roots", [])) or "(none)"
    now = datetime.now().astimezone().strftime("%A, %B %d, %Y, %I:%M %p (%Z)")
    cwd = os.getcwd()

    return _TEMPLATE.format(
        now=now,
        cwd=cwd,
        cwd_listing=_dir_listing(cwd),
        user=user,
        hostname=hostname,
        os_name=os_name,
        shell_name=shell_name,
        home=str(Path.home()),
        roots=roots,
        memory_block=_memory_block(cfg),
    )
