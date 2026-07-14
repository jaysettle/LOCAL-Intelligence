#!/usr/bin/env python3
"""
Agentic loop over Ollama /api/chat with native function calling.

run_turn() is a generator that yields typed events so the renderer can display
streaming text, thinking, and tool activity however it likes:

    ("think",        str)   incremental reasoning tokens
    ("text",         str)   incremental assistant answer tokens
    ("tool_start",   dict)  {"name","args"} — a tool is about to run
    ("tool_result",  dict)  {"name","args","result"} — tool finished
    ("error",        str)   fatal error message
    ("done",         None)  turn complete

The conversation `messages` list is mutated in place so callers keep history
across turns. Images (base64) attach to the next user message.
"""

import base64
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from .tools import OLLAMA_TOOLS, execute_tool

Event = Tuple[str, Any]

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


class AgentError(Exception):
    pass


def encode_images(paths: Optional[List[str]]) -> List[str]:
    out = []
    for p in paths or []:
        if not str(p).lower().endswith(_IMAGE_EXTS):
            continue
        try:
            out.append(base64.b64encode(Path(p).expanduser().read_bytes()).decode("ascii"))
        except Exception:
            pass
    return out


def _parse_args(raw) -> Dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
    return {}


def run_turn(
    cfg: Dict[str, Any],
    messages: List[Dict],
    user_text: str,
    image_paths: Optional[List[str]] = None,
) -> Iterator[Event]:
    """Run one user turn to completion (through any number of tool calls)."""
    user_msg: Dict[str, Any] = {"role": "user", "content": user_text}
    images = encode_images(image_paths)
    if images:
        user_msg["images"] = images
    messages.append(user_msg)

    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    max_iters = int(cfg.get("max_tool_iterations", 25))

    for _iteration in range(max_iters):
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "tools": OLLAMA_TOOLS,
            "stream": True,
            "keep_alive": cfg.get("keep_alive", "30m"),
            "options": {"num_ctx": int(cfg.get("num_ctx", 32768))},
        }

        content_acc = ""
        tool_calls: List[Dict] = []

        try:
            resp = requests.post(url, json=payload, stream=True, timeout=int(cfg.get("timeout", 600)))
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            yield ("error", f"Cannot reach Ollama at {cfg['ollama_url']}. Is it running? (Try: ollama serve)")
            return
        except Exception as e:
            yield ("error", f"Ollama request failed: {e}")
            return

        try:
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = chunk.get("message") or {}

                thinking = msg.get("thinking")
                if thinking:
                    yield ("think", thinking)

                content = msg.get("content")
                if content:
                    content_acc += content
                    yield ("text", content)

                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    tool_calls.append({"name": fn.get("name", ""), "args": _parse_args(fn.get("arguments"))})

                if chunk.get("done"):
                    break
        except Exception as e:
            yield ("error", f"Stream interrupted: {e}")
            return

        if not tool_calls:
            messages.append({"role": "assistant", "content": content_acc})
            yield ("done", None)
            return

        # Record assistant turn (with its tool calls) so the model sees its own actions.
        messages.append({
            "role": "assistant",
            "content": content_acc,
            "tool_calls": [{"function": {"name": t["name"], "arguments": t["args"]}} for t in tool_calls],
        })

        for t in tool_calls:
            name, args = t["name"], t["args"]
            yield ("tool_start", {"name": name, "args": args})
            result = execute_tool(name, args)
            yield ("tool_result", {"name": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_name": name, "content": result})

    yield ("text", "\n\n_(stopped: reached the tool-call limit for one message)_\n")
    yield ("done", None)
