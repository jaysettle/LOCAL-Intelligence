#!/usr/bin/env python3
"""Dispatch a tool call to its implementation and return a string result."""

from typing import Any, Dict

from .file_tools import read_file, write_file, glob_files, list_directory
from .shell_tools import shell, grep
from .web_tools import web_search, web_fetch

_DISPATCH = {
    "read_file": read_file,
    "write_file": write_file,
    "shell": shell,
    "glob": glob_files,
    "grep": grep,
    "list_directory": list_directory,
    "web_search": web_search,
    "web_fetch": web_fetch,
}


def execute_tool(name: str, tool_input: Dict[str, Any]) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"Error: Unknown tool '{name}'"
    try:
        return fn(tool_input)
    except Exception as e:  # defensive: a tool must never crash the loop
        return f"Error executing {name}: {e}"
