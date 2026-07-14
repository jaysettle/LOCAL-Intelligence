#!/usr/bin/env python3
"""
Agentic loop over Ollama /api/chat with native function calling.

run_turn() is a generator that yields typed events so the renderer can display
streaming text, thinking, and tool activity however it likes:

    ("think",        str)   incremental reasoning tokens
    ("text",         str)   incremental assistant answer tokens
    ("tool_start",   dict)  {"name","args"} — a tool is about to run
    ("tool_result",  dict)  {"name","args","result"} — tool finished
    ("notice",       str)   a dim status line (compaction, loop warnings)
    ("error",        str)   fatal error message
    ("done",         None)  turn complete

The conversation `messages` list is mutated in place so callers keep history
across turns. Images (base64) attach to the next user message.

Small-model reliability harness (context compaction, malformed tool-call rescue,
loop detection, empty-turn nudge) is inspired by patterns in the MIT-licensed
lutelute/local-cli project.
"""

import base64
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import requests

from .tools import OLLAMA_TOOLS, execute_tool

Event = Tuple[str, Any]

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_CHARS_PER_TOKEN = 4  # rough heuristic for the compaction budget


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


def _chat_once(cfg: Dict[str, Any], messages: List[Dict], model: str) -> str:
    """A single non-streaming completion (used for compaction summaries)."""
    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": cfg.get("keep_alive", "30m"),
        "options": {"num_ctx": int(cfg.get("num_ctx", 32768))},
    }
    resp = requests.post(url, json=payload, timeout=int(cfg.get("timeout", 600)))
    resp.raise_for_status()
    return (resp.json().get("message") or {}).get("content", "")


def _maybe_compact(cfg: Dict[str, Any], messages: List[Dict]) -> Optional[str]:
    """Summarize old turns when the transcript nears the context budget.

    Cuts at the last user-message boundary so a tool message is never orphaned
    from its assistant tool_calls. Returns a notice string if it compacted.
    """
    num_ctx = int(cfg.get("num_ctx", 32768))
    ratio = float(cfg.get("compact_at_ratio", 0.75))
    budget_chars = num_ctx * _CHARS_PER_TOKEN * ratio

    total = sum(len(str(m.get("content", ""))) for m in messages)
    if total < budget_chars or len(messages) < 8:
        return None

    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
    if last_user is None or last_user <= 1:
        return None
    middle = messages[1:last_user]
    if len(middle) < 4:
        return None

    convo = "\n".join(f"{m.get('role')}: {m.get('content', '')}" for m in middle if m.get("content"))
    prompt = (
        "Summarize this earlier part of a conversation in 5-8 concise bullet points. "
        "Preserve decisions made, file paths touched, and any facts needed to continue:\n\n" + convo
    )
    model = cfg.get("fast_model") or cfg["model"]
    try:
        summary = _chat_once(cfg, [{"role": "user", "content": prompt}], model)
    except Exception:
        return None  # compaction is best-effort; keep going uncompacted
    if not summary.strip():
        return None

    messages[:] = (
        [messages[0], {"role": "system", "content": "[Summary of earlier conversation]\n" + summary}]
        + messages[last_user:]
    )
    return f"compacted {len(middle)} earlier messages to fit the context window"


def run_turn(
    cfg: Dict[str, Any],
    messages: List[Dict],
    user_text: str,
    image_paths: Optional[List[str]] = None,
    approver=None,
) -> Iterator[Event]:
    """Run one user turn to completion (through any number of tool calls).

    approver: optional callable (name, args) -> bool. When set, mutating tools
    (write_file, edit_file, delete_file, shell) are gated on its approval.
    """
    user_msg: Dict[str, Any] = {"role": "user", "content": user_text}
    images = encode_images(image_paths)
    if images:
        user_msg["images"] = images
    messages.append(user_msg)

    notice = _maybe_compact(cfg, messages)
    if notice:
        yield ("notice", notice)

    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    max_iters = int(cfg.get("max_tool_iterations", 25))
    mutating = {"write_file", "edit_file", "delete_file", "shell"}

    recent_sigs: List[str] = []  # for loop detection
    empty_nudges = 0

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
            # Empty turn (no answer, no tools): nudge once before giving up.
            if not content_acc.strip() and empty_nudges < 1:
                empty_nudges += 1
                messages.append({"role": "assistant", "content": ""})
                messages.append({
                    "role": "user",
                    "content": "You returned nothing. Either call a tool to make progress or give your final answer now.",
                })
                continue
            messages.append({"role": "assistant", "content": content_acc})
            yield ("done", None)
            return

        # Record the assistant turn (with its tool calls) so the model sees its own actions.
        messages.append({
            "role": "assistant",
            "content": content_acc,
            "tool_calls": [{"function": {"name": t["name"], "arguments": t["args"]}} for t in tool_calls],
        })

        for t in tool_calls:
            name, args = t["name"], t["args"]

            # Rescue malformed tool-call arguments (bad JSON) instead of executing garbage.
            if isinstance(args, dict) and "_raw" in args:
                yield ("tool_start", {"name": name, "args": args})
                result = (
                    "Error: your tool arguments were not valid JSON. Re-issue the call with a "
                    "proper JSON object for the arguments."
                )
                yield ("tool_result", {"name": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_name": name, "content": result})
                continue

            # Loop detection: same call+args repeated too many times.
            sig = name + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            recent_sigs.append(sig)
            if recent_sigs.count(sig) >= 3:
                result = (
                    "Notice: you have already run this exact tool call twice with no change in result. "
                    "Stop repeating it — try a different approach or give your final answer."
                )
                yield ("notice", f"loop detected on {name}; nudging the model to change approach")
                yield ("tool_result", {"name": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_name": name, "content": result})
                continue

            # Approval gate for mutating actions.
            if approver is not None and name in mutating:
                if not approver(name, args):
                    result = "The user declined to run this action."
                    yield ("tool_result", {"name": name, "args": args, "result": result})
                    messages.append({"role": "tool", "tool_name": name, "content": result})
                    continue

            yield ("tool_start", {"name": name, "args": args})
            result = execute_tool(name, args)
            yield ("tool_result", {"name": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_name": name, "content": result})

    yield ("text", "\n\n_(stopped: reached the tool-call limit for one message)_\n")
    yield ("done", None)
