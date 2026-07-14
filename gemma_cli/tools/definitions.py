#!/usr/bin/env python3
"""
Tool definitions for the local agent.

Schemas are declared once in Anthropic-style ("input_schema") and converted to
the OpenAI/Ollama function-calling shape. The shell tool's description adapts to
the host OS so the model knows whether it's driving PowerShell or bash.
"""

import platform

_IS_WINDOWS = platform.system() == "Windows"
_SHELL_NAME = "PowerShell" if _IS_WINDOWS else "bash"
_SHELL_EXAMPLE = "Get-ChildItem C:\\Users" if _IS_WINDOWS else "ls ~/"

TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a text file. Returns the file with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read (absolute or ~-relative)"},
                "offset": {"type": "integer", "description": "1-indexed line to start from. Optional.", "default": 1},
                "limit": {"type": "integer", "description": "Max lines to read. Optional, default 500.", "default": 500},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a WHOLE file, creating it (and parent folders) if needed, overwriting if it exists. Use this for NEW files or full rewrites. To change PART of an existing file, use edit_file instead — it is safer and cheaper. Restricted to allowed write roots.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write"},
                "content": {"type": "string", "description": "Full content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Change part of an existing file by exact string replacement. PREFER this over write_file for edits — you only supply the small piece that changes, so nothing else is lost. old_string must match the file exactly (including whitespace/indentation) and be unique unless replace_all is true. Read the file first to copy the exact text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "old_string": {"type": "string", "description": "Exact text to find (copy it verbatim from the file, including indentation)"},
                "new_string": {"type": "string", "description": "Text to replace it with"},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring a unique match. Default false.", "default": False},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "delete_file",
        "description": "Delete a file or folder by sending it to the OS trash / recycle bin (recoverable). Prefer this over 'rm' or 'Remove-Item' in the shell so deletions can be undone. Restricted to allowed write roots.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to delete"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "shell",
        "description": (
            f"Execute a {_SHELL_NAME} command on this computer and return its output. "
            f"Use for running programs, inspecting the system, moving/deleting files, git, etc. "
            f"Example: `{_SHELL_EXAMPLE}`. Commands are non-interactive; keep them short."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": f"The {_SHELL_NAME} command to run"},
                "timeout": {"type": "integer", "description": "Timeout in seconds. Default 60.", "default": 60},
            },
            "required": ["command"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern (e.g. '**/*.py'). Returns matching paths, newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern like '**/*.txt'"},
                "path": {"type": "string", "description": "Base directory to search. Defaults to the current working directory.", "default": "."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Search file contents for a regex pattern. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for"},
                "path": {"type": "string", "description": "File or directory to search. Defaults to current directory.", "default": "."},
                "file_pattern": {"type": "string", "description": "Optional glob to filter files, e.g. '*.py'", "default": "*"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and folders in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list. Defaults to the current working directory.", "default": "."},
            },
            "required": [],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Search the web (via a local SearXNG instance). Returns titles, URLs and snippets. "
            "Use for current events, versions, prices, docs — anything not in your training data. "
            "Follow up with web_fetch to read a promising result in full."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {"type": "integer", "description": "Results to return (1-10). Default 6.", "default": 6},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch a web page and return its readable text. Use after web_search to read a result in full.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http(s) URL to fetch"},
                "max_chars": {"type": "integer", "description": "Max characters to return (up to 20000). Default 8000.", "default": 8000},
            },
            "required": ["url"],
        },
    },
    {
        "name": "remember",
        "description": "Save a durable fact to memory so you recall it in future sessions. Use for project conventions, user preferences, or things you were told to remember. 'project' scope writes to GEMMA.md in this folder; 'global' scope applies everywhere.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The fact to remember, as a short sentence"},
                "scope": {"type": "string", "enum": ["project", "global"], "description": "'project' (this folder) or 'global' (everywhere). Default project.", "default": "project"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "set_plan",
        "description": "For a multi-step task, record your plan as a checklist BEFORE starting. Keeps you on track. Call complete_step as you finish each item.",
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {"type": "array", "items": {"type": "string"}, "description": "Ordered list of short step descriptions"},
            },
            "required": ["steps"],
        },
    },
    {
        "name": "complete_step",
        "description": "Mark a plan step done (1-based index) and see the updated checklist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "1-based index of the step to mark complete"},
            },
            "required": ["index"],
        },
    },
]

# Ollama / OpenAI function-calling shape derived from the schemas above.
OLLAMA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOLS
]
