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


def _chat_once(cfg: Dict[str, Any], messages: List[Dict], model: str, on_token=None) -> str:
    """A single completion, returned whole (compaction summaries, skill capture).

    This streams even though the caller wants one string. With `stream: false`
    Ollama sends nothing at all until generation finishes, so requests' read
    timeout has to cover the entire generation — and on hardware where the model
    spills to CPU that blows past any sane timeout and the whole result is lost.
    Streaming makes the timeout mean what it should: the gap between chunks.

    `on_token` receives text as it arrives, so a caller can show progress.
    """
    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "keep_alive": cfg.get("keep_alive", "30m"),
        "options": {"num_ctx": int(cfg.get("num_ctx", 32768))},
    }
    if not cfg.get("thinking", True):
        payload["think"] = False
    resp = requests.post(url, json=payload, stream=True, timeout=int(cfg.get("timeout", 600)))
    resp.raise_for_status()

    parts: List[str] = []
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        piece = (chunk.get("message") or {}).get("content")
        if piece:
            parts.append(piece)
            if on_token is not None:
                try:
                    on_token(piece)
                except Exception:
                    pass
        if chunk.get("done"):
            break
    return "".join(parts)


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
    cancel=None,
    *,
    tools: Optional[List[Dict]] = None,
    max_iters: Optional[int] = None,
    think: Optional[bool] = None,
) -> Iterator[Event]:
    """Run one user turn to completion (through any number of tool calls).

    tools / max_iters / think override the defaults for one call. Child runs
    (see run_child) use them to give a subtask a narrower tool set, a smaller
    iteration budget, and thinking off.

    approver: optional callable (name, args) -> bool. When set, mutating tools
    (write_file, edit_file, delete_file, shell) are gated on its approval.
    cancel: optional threading.Event. When set mid-turn, the current response is
    stopped cooperatively (the HTTP stream is closed) and the turn ends.
    """
    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()
    user_msg: Dict[str, Any] = {"role": "user", "content": user_text}
    images = encode_images(image_paths)
    if images:
        user_msg["images"] = images
    messages.append(user_msg)

    notice = _maybe_compact(cfg, messages)
    if notice:
        yield ("notice", notice)

    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    max_iters = int(max_iters if max_iters is not None else cfg.get("max_tool_iterations", 25))
    tools = OLLAMA_TOOLS if tools is None else tools
    # Only ever SEND the field when disabling: models without a thinking mode
    # reject it, and the default must keep working for them.
    think_on = cfg.get("thinking", True) if think is None else bool(think)
    mutating = {"write_file", "edit_file", "delete_file", "shell"}

    recent_sigs: List[str] = []  # for loop detection
    empty_nudges = 0

    for _iteration in range(max_iters):
        payload = {
            "model": cfg["model"],
            "messages": messages,
            "tools": tools,
            "stream": True,
            "keep_alive": cfg.get("keep_alive", "30m"),
            "options": {"num_ctx": int(cfg.get("num_ctx", 32768))},
        }
        if not think_on:
            payload["think"] = False

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
                if _cancelled():
                    try:
                        resp.close()
                    except Exception:
                        pass
                    messages.append({"role": "assistant", "content": content_acc})
                    yield ("notice", "stopped")
                    yield ("done", None)
                    return
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
            if _cancelled():
                yield ("notice", "stopped")
                yield ("done", None)
                return
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


# ---------------------------------------------------------------------------
# Child runs — the primitive under every form of "recursion" here
# ---------------------------------------------------------------------------

# Tools a child may never call. Empty today; a model-callable `delegate` tool
# goes here the day it exists, which is what caps recursion depth at 1 in code
# rather than in a prompt.
CHILD_EXCLUDED_TOOLS = frozenset()

# What a read-only child may not do. Observed live: given a per-file skill whose
# last step was "write everything to INDEX.md", the FIRST child wrote INDEX.md
# with its one line - and the next child would have overwritten it. Writing is
# the synthesis turn's job; children read.
MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete_file", "shell"})


def run_child(
    cfg: Dict[str, Any],
    task: str,
    *,
    label: str = "",
    context: str = "",
    approver=None,
    cancel=None,
    out: Optional[Dict[str, Any]] = None,
    readonly: bool = False,
) -> Iterator[Event]:
    """Run one scoped subtask in a FRESH context and hand back only its result.

    readonly=True withholds MUTATING_TOOLS from the child entirely - enforced by
    the tool list, not by asking nicely.

    Why this exists: there is one GPU, so a second context buys no speed. What it
    buys is isolation. Reading twelve documents inside the parent turn fills a
    32K window; reading them in twelve children leaves the parent with twelve
    result lines. The child's transcript is discarded on purpose.

    The child gets its own system prompt (cwd, memory, skill index — the fixed
    cost of a child, ~2-3K tokens), the task, no parent history, a smaller
    iteration budget, thinking OFF by default (measured: thinking is ~85% of a
    trivial turn on an 8 GB box, and child work is mechanical), and every tool
    except CHILD_EXCLUDED_TOOLS. It inherits the approver gate and cancel event.

    Yields ("child", (kind, payload)) for each of the child's own events so a
    renderer can show them indented, then ("child_result", {"label", "result"}).
    The result is capped at child_result_chars. If `out` is given, the result is
    also stored in out["result"] for callers iterating the events elsewhere.
    """
    from .sysprompt import build_system_prompt

    child_cfg = dict(cfg)
    child_cfg["thinking"] = bool(cfg.get("child_thinking", False))
    blocked = set(CHILD_EXCLUDED_TOOLS)
    if readonly:
        blocked |= MUTATING_TOOLS
    tools = [t for t in OLLAMA_TOOLS if t["function"]["name"] not in blocked]
    iters = int(cfg.get("child_max_tool_iterations", 10))
    cap = int(cfg.get("child_result_chars", 2000))

    messages: List[Dict] = [{"role": "system", "content": build_system_prompt(child_cfg)}]
    prompt = f"{context.strip()}\n\n{task.strip()}" if context.strip() else task.strip()

    final: List[str] = []
    for kind, payload in run_turn(child_cfg, messages, prompt, approver=approver, cancel=cancel,
                                  tools=tools, max_iters=iters):
        if kind == "text":
            final.append(payload)
        yield ("child", (kind, payload))

    result = "".join(final).strip()
    if len(result) > cap:
        result = result[:cap].rstrip() + " …(truncated)"
    if out is not None:
        out["result"] = result
    yield ("child_result", {"label": label, "result": result})
