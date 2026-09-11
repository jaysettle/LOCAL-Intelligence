#!/usr/bin/env python3
"""
LOCAL-Intelligence CLI entry point.

  gemma go                      interactive chat session in the current folder
  gemma go C:\\path\\to\\project interactive chat session in a specific folder
  gemma                         interactive chat session (same as `go`)
  gemma "quick question"        one-shot, prints answer and exits
  gemma -p "prompt"             one-shot (explicit flag form)
  gemma -i img.png -p "..."     attach an image (vision)
  gemma --model gemma4:e4b      override model for this run
  gemma update                  pull the latest source and reinstall, then exit
  gemma update --check          say whether an update is available, install nothing
  gemma skills [folder]         list saved skills and their usage, then exit
  gemma --setup-config          write a default config.yaml and exit

Interactive REPL: input stays live while it answers; type + Enter to queue the
next prompt; Esc stops the current answer; Alt+V pastes a clipboard image; a
status line under the input shows the folder + GPU/CPU/VRAM while it works.
REPL commands: /paste /image /clear /model /save /sessions /resume /memory /skills
/skill new <name> /<skill-name> /help /exit
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console

from . import __version__
from .agent import run_turn
from .config import load_config, write_default_config, apply_to_tools, CONFIG_PATH
from .render import Renderer
from .sysprompt import build_system_prompt
from .sessions import (
    new_session_path, save_session, latest_session, load_session,
    list_sessions, summarize,
)


def _preflight(cfg: Dict, console: Console) -> None:
    """Warn early if Ollama or the model isn't available (non-fatal)."""
    import requests
    try:
        r = requests.get(f"{cfg['ollama_url'].rstrip('/')}/api/tags", timeout=5)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if cfg["model"] not in models:
            console.print(
                f"[yellow]Note:[/yellow] model [bold]{cfg['model']}[/bold] not found in Ollama. "
                f"Pull it with: [bold]ollama pull {cfg['model']}[/bold]"
            )
    except Exception:
        console.print(
            f"[yellow]Note:[/yellow] couldn't reach Ollama at {cfg['ollama_url']}. "
            "Start it (ollama serve / the Ollama app) before chatting."
        )


def _render_turn(cfg, messages, console, renderer, events, approver=None) -> None:
    """Render one turn's events in the plain REPL / one-shot path.

    Ctrl+C here stops the ANSWER, not the session. Without this, an interrupt
    mid-generation escaped as a KeyboardInterrupt traceback and took the whole
    conversation with it — and on a box where a turn is minutes long, a way to
    bail out of a runaway answer is not optional.
    """
    from .statusline import StatusBar

    # Bottom status bar (folder / GPU / VRAM / CPU / model) while the model
    # works, the same one --live shows. Not with an approver: its y/N prompt
    # cannot share the terminal with a rich Live. StatusBar is a no-op when
    # stdout is not a terminal or status_line is off in config.
    bar = StatusBar(console, cfg) if approver is None else None
    use_bar = bar is not None and bar.enabled
    prev_whole = renderer.whole_lines
    try:
        if use_bar:
            renderer.whole_lines = True  # a Live must only ever see whole lines above it
            with bar:
                renderer.consume(events)
        else:
            renderer.consume(events)
    except KeyboardInterrupt:
        try:
            events.close()  # closes the HTTP stream via the generator's finally/GC
        except Exception:
            pass
        # Keep the transcript well-formed: the user message must not be left
        # dangling without an assistant turn, or the next request is malformed.
        if messages and messages[-1].get("role") == "user":
            messages.append({"role": "assistant", "content": "(stopped by the user before finishing)"})
        console.print("\n[dim]· stopped (Ctrl+C) — session kept; Ctrl+C again at the prompt to exit[/dim]")
    finally:
        renderer.whole_lines = prev_whole


def _run_once(cfg, messages, console, renderer, text, images=None, approver=None) -> None:
    """One user turn in the plain REPL / one-shot path."""
    events = run_turn(cfg, messages, text, image_paths=images, approver=approver)
    _render_turn(cfg, messages, console, renderer, events, approver=approver)


def _per_item_events(cfg, messages, job, approver=None, cancel=None):
    """A per-file skill run as one event stream: N child runs, then a synthesis turn.

    Control flow lives HERE, in Python — the model never decides to recurse. Each
    file is processed in a fresh child context (see agent.run_child) and only its
    result line reaches the parent, which then finishes the task in a normal turn
    that is recorded in the session like any other.
    """
    from .agent import run_child

    skill, items, extra = job["skill"], job["items"], job.get("extra", "")
    readonly = bool(cfg.get("child_readonly", True)) and not skill.child_writes
    notes = ("" if cfg.get("child_thinking", False) else ", thinking off") + (", read-only" if readonly else "")
    yield ("notice", f"{skill.name}: {len(items)} file(s), each in its own context{notes}")

    results = []
    for i, path in enumerate(items, 1):
        if cancel is not None and cancel.is_set():
            yield ("notice", "stopped before all files were processed")
            return
        holder: Dict = {}
        label = f"[{i}/{len(items)}] {path.name}"
        for event in run_child(cfg, skill.render_item(path, extra), label=label,
                               approver=approver, cancel=cancel, out=holder, readonly=readonly):
            yield event
        results.append((path.name, holder.get("result", "")))

    for event in run_turn(cfg, messages, skill.render_synthesis(results, extra),
                          approver=approver, cancel=cancel):
        yield event


def _run_per_item(cfg, messages, console, renderer, job, approver=None) -> None:
    events = _per_item_events(cfg, messages, job, approver=approver)
    _render_turn(cfg, messages, console, renderer, events, approver=approver)


def _make_approver(console: Console):
    """Return a y/N prompt callback for gating mutating tool calls."""
    from .render import _arg_preview

    def approver(name, args) -> bool:
        try:
            ans = console.input(
                f"[yellow]Approve[/yellow] [bold]{name}[/bold] [dim]{_arg_preview(args)}[/dim] ? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    return approver


# ---------------------------------------------------------------------------
# Clipboard helpers
# ---------------------------------------------------------------------------

def _grab_clipboard_to_file():
    """Return (path, error). A temp/real image file from the clipboard, or None.

    Handles both a bitmap on the clipboard (Win+Shift+S snip, or Ctrl+C from an
    image app) and image file(s) copied in Explorer.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return None, "Pillow not installed (pip install pillow)"
    try:
        data = ImageGrab.grabclipboard()
    except Exception as e:
        return None, f"clipboard read failed: {e}"
    if data is None:
        return None, "no image on the clipboard"
    if isinstance(data, list):
        imgs = [p for p in data if str(p).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))]
        return (imgs[0], None) if imgs else (None, "clipboard has files but no image")
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="gemma_paste_", suffix=".png")
        os.close(fd)
        data.save(tmp, "PNG")
        return tmp, None
    except Exception as e:
        return None, f"could not save pasted image: {e}"


# ---------------------------------------------------------------------------
# Shared command handling
# ---------------------------------------------------------------------------

def session_dir_lookup(name: str):
    """Resolve a session name/filename to a path in this folder's session dir."""
    from .sessions import session_dir
    d = session_dir()
    for cand in (d / name, d / f"{name}.json"):
        if cand.exists():
            return cand


_HELP = (
    "[dim]/paste [prompt] — attach the clipboard image (Alt+V does this live)\n"
    "/image <path> <prompt> — attach an image file\n"
    "/clear — reset conversation\n"
    "/model <tag> — switch model\n"
    "/save — save this session now\n"
    "/sessions — list saved sessions in this folder\n"
    "/resume [name] — load the latest (or named) session\n"
    "/memory [global] — open the memory file you can edit by hand\n"
    "/skills — list saved skills and how often they're used\n"
    "/skill new <name> — turn what we just did into a reusable skill\n"
    "/skill edit|delete <name> — manage a skill\n"
    "/<skill-name> [extra] — run a saved skill (per-file skills run once per file)\n"
    "/check — have the model review its last answer against the tool results\n"
    "/exit — quit    (while answering: Esc stops it, typing queues the next)[/dim]"
)


def _handle_memory(parts, cfg, console):
    """`/memory` — open the human-writable memory file in an editor."""
    from . import skills as skills_mod

    scope = parts[1].lower() if len(parts) >= 2 else "project"
    if scope == "global":
        path = Path(cfg.get("global_memory_file") or "")
    else:
        path = Path.cwd() / cfg.get("project_memory_file", "GEMMA.md")

    existed = path.exists()
    console.print(f"[dim]{skills_mod.open_in_editor(path)}[/dim]")
    if not existed:
        console.print(
            "[dim]This file is loaded into the agent's context every run. Write plain "
            "markdown — conventions, gotchas, who's who. It also appends here itself "
            "when it learns something durable.[/dim]"
        )
    console.print("[dim]Changes apply on the next launch (the system prompt is built at startup).[/dim]")
    return ("handled", None, None)


def _skill_report(console) -> None:
    from . import skills as skills_mod

    rows = skills_mod.report()
    if not rows:
        console.print(
            f"[dim]No skills yet. Create one from work you just did with "
            f"[/dim][bold]/skill new <name>[/bold][dim], or write a markdown file in "
            f"{skills_mod.project_skills_dir()}[/dim]"
        )
        return

    console.print(f"[bold]{len(rows)} skill(s)[/bold]")
    for row in rows:
        used = f"used {row['count']}×" if row["count"] else "unused"
        when = f" [dim]· when: {row['when']}[/dim]" if row["when"] else ""
        console.print(f"  [bold cyan]/{row['name']}[/bold cyan] [dim]({row['scope']}, {used})[/dim]")
        if row["description"]:
            console.print(f"      {row['description']}{when}")
        if row["last_used"]:
            by = row.get("sources") or {}
            detail = ", ".join(f"{k}: {v}" for k, v in by.items())
            console.print(f"      [dim]last used {row['last_used']}" + (f" ({detail})" if detail else "") + "[/dim]")
    console.print(f"[dim]Project skills live in {skills_mod.project_skills_dir()}[/dim]")


def _capture_skill(name, cfg, messages, console):
    """`/skill new <name>` — write the work we just did up as a reusable skill."""
    from . import skills as skills_mod
    from .agent import _chat_once

    if not skills_mod.is_valid_name(name):
        console.print(f"[red]'{name}' is not a usable skill name[/red] [dim](letters, digits, - and _)[/dim]")
        return ("handled", None, None)

    transcript = skills_mod.transcript_for_capture(messages)
    if len(transcript) < 200:
        console.print("[yellow]Not enough conversation yet to turn into a skill. Do the task first, then run this.[/yellow]")
        return ("handled", None, None)

    console.print("[dim]writing the skill from this conversation (this can take a minute)…[/dim]")
    prompt = skills_mod._CAPTURE_PROMPT.format(transcript=transcript)

    # Show progress: on a CPU-spilled local model this runs for minutes, and a
    # silent terminal is indistinguishable from a hang.
    state = {"chars": 0, "dots": 0}

    def tick(piece: str) -> None:
        state["chars"] += len(piece)
        while state["chars"] // 80 > state["dots"]:
            state["dots"] += 1
            print(".", end="", flush=True)

    raw = ""
    error = None
    try:
        raw = _chat_once(cfg, [{"role": "user", "content": prompt}],
                         cfg.get("fast_model") or cfg["model"], on_token=tick)
    except Exception as e:
        error = e
    if state["dots"]:
        print(flush=True)

    parsed = skills_mod.parse_capture(raw) if raw.strip() else {"description": "", "when": "", "body": ""}

    if not parsed["body"].strip():
        # Never discard the user's work just because the model call failed —
        # save the transcript as a draft they can edit into a real skill.
        reason = f"the model call failed ({error})" if error else "the model returned nothing usable"
        console.print(f"[yellow]{reason}.[/yellow]")
        draft = (
            "DRAFT - the model could not write this up, so here is the raw transcript.\n"
            "Edit it into numbered steps and delete this notice.\n\n"
            "```\n" + transcript[-6000:] + "\n```\n"
        )
        try:
            path = skills_mod.save(name, f"DRAFT captured from a session", draft)
        except (ValueError, OSError) as e:
            console.print(f"[red]could not save a draft either: {e}[/red]")
            return ("handled", None, None)
        console.print(f"[green]saved a draft[/green] [dim]{path}[/dim] [dim]- your work is not lost.[/dim]")
        console.print(f"[dim]{skills_mod.open_in_editor(path)}[/dim]")
        return ("handled", None, None)

    try:
        path = skills_mod.save(
            name,
            parsed["description"] or f"Saved from a session on {__import__('datetime').date.today()}",
            parsed["body"],
            when=parsed["when"],
        )
    except (ValueError, OSError) as e:
        console.print(f"[red]{e}[/red]")
        return ("handled", None, None)

    console.print(f"[green]saved skill[/green] [bold]/{name}[/bold] [dim]→ {path}[/dim]")
    console.print("[dim]Read it and fix anything wrong — it's plain markdown, and the model "
                  "only saw the transcript. Available as a command on the next launch.[/dim]")
    console.print(f"[dim]{skills_mod.open_in_editor(path)}[/dim]")
    return ("handled", None, None)


def _handle_skill(parts, line, cfg, messages, console):
    """`/skill new|edit|delete <name>`."""
    from . import skills as skills_mod

    sub = parts[1].lower() if len(parts) >= 2 else ""
    name = parts[2].split()[0] if len(parts) >= 3 else ""

    if sub in ("new", "save", "capture"):
        if not name:
            console.print("[red]usage: /skill new <name>[/red]")
            return ("handled", None, None)
        return _capture_skill(name, cfg, messages, console)

    if sub in ("edit", "delete", "rm") and name:
        skill = skills_mod.get(name)
        if not skill:
            console.print(f"[red]no skill named '{name}'[/red] [dim](/skills to list)[/dim]")
            return ("handled", None, None)
        if sub == "edit":
            console.print(f"[dim]{skills_mod.open_in_editor(skill.path)}[/dim]")
        else:
            from .tools.file_tools import delete_file
            console.print(f"[dim]{delete_file({'path': str(skill.path)})}[/dim]")
        return ("handled", None, None)

    console.print("[dim]usage: /skill new <name> | /skill edit <name> | /skill delete <name>[/dim]")
    return ("handled", None, None)


def _handle_command(line, cfg, messages, console, session_path):
    """Handle a /command. Returns (action, prompt_text, images):
    ('exit',None,None) | ('handled',None,None) | ('run', text, [paths])."""
    parts = line.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd in ("/exit", "/quit"):
        return ("exit", None, None)
    if cmd == "/help":
        console.print(_HELP)
        return ("handled", None, None)
    if cmd == "/clear":
        messages[:] = [messages[0]]
        console.print("[dim]conversation cleared[/dim]")
        return ("handled", None, None)
    if cmd == "/model":
        if len(parts) < 2:
            console.print(f"[dim]current model: {cfg['model']}[/dim]")
        else:
            cfg["model"] = parts[1]
            console.print(f"[dim]model set to {cfg['model']}[/dim]")
        return ("handled", None, None)
    if cmd == "/save":
        save_session(session_path, messages, cfg["model"])
        console.print(f"[dim]saved {session_path.name}[/dim]")
        return ("handled", None, None)
    if cmd == "/sessions":
        sessions = list_sessions()
        if not sessions:
            console.print("[dim]no saved sessions in this folder[/dim]")
        else:
            for p, meta in sessions[:15]:
                console.print(f"[dim]{summarize(p, meta)}[/dim]")
        return ("handled", None, None)
    if cmd == "/resume":
        target = session_dir_lookup(parts[1]) if len(parts) >= 2 else latest_session()
        if not target:
            console.print("[red]no matching session[/red]")
            return ("handled", None, None)
        prior = load_session(target)
        messages[:] = [messages[0]] + prior
        console.print(f"[dim]resumed {target.name} ({len(prior)} messages)[/dim]")
        return ("handled", None, None)
    if cmd == "/image":
        if len(parts) < 3:
            console.print("[red]usage: /image <path> <prompt>[/red]")
            return ("handled", None, None)
        return ("run", parts[2], [parts[1]])
    if cmd == "/paste":
        rest = line.split(maxsplit=1)
        prompt_text = rest[1] if len(rest) > 1 else "What is in this image? Describe it."
        path, err = _grab_clipboard_to_file()
        if not path:
            console.print(f"[yellow]{err}. Snip with Win+Shift+S or copy an image, then retry.[/yellow]")
            return ("handled", None, None)
        return ("run", prompt_text, [path])

    if cmd == "/memory":
        return _handle_memory(parts, cfg, console)
    if cmd == "/check":
        from .review import check_last_turn
        check_last_turn(cfg, messages, console)
        return ("handled", None, None)
    if cmd == "/skills":
        _skill_report(console)
        return ("handled", None, None)
    if cmd == "/skill":
        return _handle_skill(parts, line, cfg, messages, console)

    # Anything else may be a saved skill: /<skill-name> [extra context]
    from . import skills as skills_mod
    available = skills_mod.discover()
    skill = available.get(cmd[1:])
    if skill is not None:
        extra = line.split(maxsplit=1)[1] if len(line.split(maxsplit=1)) > 1 else ""
        skills_mod.record_use(skill.name, source="command")
        if skill.per_file:
            limit = int(cfg.get("per_file_max_items", 12) or 12)
            items, total = skills_mod.discover_items(skill, limit=limit)
            if not items:
                console.print(f"[yellow]{skill.name}: no files match {skill.globs or ['*']} in this folder[/yellow]")
                return ("handled", None, None)
            console.print(
                f"[dim]running skill [/dim][bold]{skill.name}[/bold][dim] ({skill.scope}) "
                f"per file: {len(items)} file(s), each in its own context[/dim]"
            )
            if total > len(items):
                console.print(f"[yellow]· only the first {len(items)} of {total} matching files "
                              f"(per_file_max_items); narrow the glob or raise the limit[/yellow]")
            return ("per_item", {"skill": skill, "items": items, "extra": extra}, None)
        console.print(f"[dim]running skill [/dim][bold]{skill.name}[/bold][dim] ({skill.scope})[/dim]")
        return ("run", skill.render(extra), None)

    console.print(f"[red]unknown command: {cmd}[/red] [dim](/help)[/dim]")
    if available:
        console.print(f"[dim]saved skills: {', '.join('/' + n for n in sorted(available))}[/dim]")
    return ("handled", None, None)


def _banner(cfg, console) -> None:
    console.print(f"[bold]LOCAL-Intelligence[/bold] [dim]v{__version__}[/dim] — model [cyan]{cfg['model']}[/cyan]")
    console.print(f"[dim]working dir:[/dim] [green]{os.getcwd()}[/green]")


# ---------------------------------------------------------------------------
# Plain REPL (non-interactive stdin, or when approval prompts are active)
# ---------------------------------------------------------------------------

def _pending_input_lines() -> List[str]:
    """Lines already sitting in the console input buffer - i.e. the rest of a paste.

    input() returns one line, so a pasted multi-line prompt used to run as one
    prompt PER LINE: the first line alone ("its contents must be exactly this:")
    sent a thinking model into a minutes-long spiral about the missing text, and
    every other line then ran as its own turn. A paste lands in the buffer all
    at once, so anything still readable immediately after input() returned is
    the rest of it. Only ever used on a real terminal: with piped stdin the
    line-by-line contract must hold (scripts and tests rely on it).
    """
    lines: List[str] = []
    try:
        if not sys.stdin.isatty():
            return lines
        if sys.platform == "win32":
            import msvcrt
            while msvcrt.kbhit():
                lines.append(input())
        else:
            import select
            while select.select([sys.stdin], [], [], 0.05)[0]:
                nxt = sys.stdin.readline()
                if not nxt:
                    break
                lines.append(nxt.rstrip("\r\n"))
    except Exception:
        pass
    return lines


def _read_prompt(console) -> str:
    """One prompt from the plain REPL, joining a pasted block into one prompt.

    Commands (/...) stay single-line so "/doc-index" pasted above "/check" runs
    as two commands, not one command with junk arguments.
    """
    line = console.input("[bold green]>[/bold green] ").strip()
    if not line or line.startswith("/"):
        return line
    rest = _pending_input_lines()
    if rest:
        line = "\n".join([line] + [r.rstrip() for r in rest]).strip()
    return line


def _repl_plain(cfg, messages, console, renderer, session_path, approver=None) -> int:
    _banner(cfg, console)
    console.print("[dim]Type your message. /help for commands, /exit to quit.[/dim]\n")
    _preflight(cfg, console)

    while True:
        try:
            line = _read_prompt(console)
        except (EOFError, KeyboardInterrupt):
            # A second Ctrl+C can land while rich is measuring the terminal for
            # this very print; that used to escape as a traceback. Plain print
            # cannot be interrupted in an interesting way.
            try:
                console.print("\n[dim]bye[/dim]")
            except BaseException:
                print("\nbye")
            return 0
        if not line:
            continue
        if line.startswith("/"):
            action, ptext, imgs = _handle_command(line, cfg, messages, console, session_path)
            if action == "exit":
                console.print("[dim]bye[/dim]")
                return 0
            if action == "run":
                _run_once(cfg, messages, console, renderer, ptext, images=imgs, approver=approver)
                save_session(session_path, messages, cfg["model"])
            elif action == "per_item":
                _run_per_item(cfg, messages, console, renderer, ptext, approver=approver)
                save_session(session_path, messages, cfg["model"])
            continue
        _run_once(cfg, messages, console, renderer, line, approver=approver)
        save_session(session_path, messages, cfg["model"])


# ---------------------------------------------------------------------------
# Interactive REPL (live input, type-ahead queue, Esc-cancel, status line)
# ---------------------------------------------------------------------------

def _consume_plain(events, show_thinking=True, width=100) -> str:
    """Render an event stream with plain print() — reliable under patch_stdout
    and from a background thread, where rich's output gets swallowed.

    Only WHOLE lines are ever printed. prompt_toolkit's patch_stdout renders a
    complete line cleanly above the prompt, but a flushed PARTIAL line is written
    on the prompt's own row and redrawn on every refresh — so streaming tokens
    with end="" left the answer's last few words stuck bottom-left while a blank
    area grew above them. Streamed text is therefore buffered and emitted at
    newlines, or soft-wrapped at the last space once it passes `width`.
    """
    mode = None
    answer = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            print(buf, flush=True)
            buf = ""

    def push(text: str) -> None:
        nonlocal buf
        buf += text
        while True:
            nl = buf.find("\n")
            if nl != -1:
                print(buf[:nl], flush=True)
                buf = buf[nl + 1:]
                continue
            if len(buf) >= width:
                cut = buf.rfind(" ", 0, width)
                if cut <= 0:
                    cut = width
                print(buf[:cut], flush=True)
                buf = buf[cut:].lstrip(" ")
                continue
            break

    for kind, payload in events:
        if kind == "think":
            if not show_thinking:
                continue
            if mode != "think":
                flush()
                print("thinking:", flush=True)
                mode = "think"
            push(payload)
        elif kind == "text":
            if mode != "text":
                flush()
            mode = "text"
            answer.append(payload)
            push(payload)
        elif kind == "tool_start":
            flush()
            mode = None
            a = payload.get("args", {}) or {}
            prev = a.get("path") or a.get("command") or a.get("pattern") or a.get("url") or a.get("query") or ""
            print(f"* {payload['name']} {str(prev)[:80]}", flush=True)
        elif kind == "tool_result":
            r = payload.get("result", "") or ""
            first = r.strip().splitlines()[0] if r.strip() else "(no output)"
            print(f"  {first[:120]}", flush=True)
        elif kind == "notice":
            flush()
            mode = None
            print(f"- {payload}", flush=True)
        elif kind == "error":
            flush()
            mode = None
            print(f"Error: {payload}", flush=True)
        elif kind == "child":
            ck, cp = payload
            if ck == "tool_start":
                flush()
                mode = None
                a = (cp or {}).get("args", {}) or {}
                prev = a.get("path") or a.get("command") or a.get("pattern") or a.get("url") or a.get("query") or ""
                print(f"    -> {cp.get('name', '')} {str(prev)[:70]}", flush=True)
            elif ck == "error":
                flush()
                mode = None
                print(f"    -> Error: {cp}", flush=True)
        elif kind == "child_result":
            flush()
            mode = None
            res = (payload.get("result") or "").strip()
            first = res.splitlines()[0] if res else "(no result)"
            print(f"  -> {payload.get('label', '')}: {first[:110]}", flush=True)
        elif kind == "done":
            flush()
            mode = None
    flush()
    return "".join(answer)


def _repl_interactive(cfg, messages, console, renderer, session_path, approver=None) -> int:
    import queue as _queue
    import threading
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from . import statusline

    # rich's color codes leak as literal ANSI ("?[2m ... ?[0m") under
    # prompt_toolkit's patch_stdout, so this interactive path renders WITHOUT
    # color. (Color still works in one-shot and the plain reader.)
    console = Console(no_color=True, force_terminal=False)
    renderer = Renderer(console, show_thinking=renderer.show_thinking, verbose=renderer.verbose)

    _banner(cfg, console)
    console.print(
        "[dim]Enter to send. Type while it answers to queue the next prompt. "
        "Esc stops the current answer. Alt+V pastes a clipboard image. /help for commands.[/dim]\n"
    )
    _preflight(cfg, console)

    work = _queue.Queue()
    pending_images: List[str] = []
    state = {"busy": False, "cancel": None, "sample": None}
    segments = cfg.get("status_segments", ["folder", "gpu", "vram", "cpu", "model"])
    show_status = cfg.get("status_line", True)
    refresh = float(cfg.get("status_refresh", 0.5) or 0.5)

    def worker():
        while True:
            item = work.get()
            if item is None:
                return
            per_item = item if isinstance(item, dict) else None
            text, images = (None, None) if per_item else item
            cancel = threading.Event()
            state["cancel"] = cancel
            state["busy"] = True
            stop_sampler = threading.Event()

            def _sampler():
                state["sample"] = statusline.sample()
                while not stop_sampler.wait(refresh):
                    state["sample"] = statusline.sample()

            st = threading.Thread(target=_sampler, daemon=True)
            st.start()
            try:
                if per_item is not None:
                    events = _per_item_events(cfg, messages, per_item, approver=approver, cancel=cancel)
                else:
                    events = run_turn(cfg, messages, text, image_paths=images, approver=approver, cancel=cancel)
                _consume_plain(events, show_thinking=cfg.get("show_thinking", True))
            except Exception as e:
                print(f"error: {e}", flush=True)
            finally:
                stop_sampler.set()
                state["busy"] = False
                state["cancel"] = None
                state["sample"] = None
                save_session(session_path, messages, cfg["model"])
            work.task_done()

    threading.Thread(target=worker, daemon=True).start()

    kb = KeyBindings()

    @kb.add("escape", "v")  # Alt+V: paste a clipboard image
    def _paste(event):
        path, _err = _grab_clipboard_to_file()
        if path:
            pending_images.append(path)
            event.app.current_buffer.insert_text("[image] ")
        else:
            event.app.output.bell()

    @kb.add("escape")  # Esc: stop the current response (queue survives)
    def _cancel(event):
        c = state["cancel"]
        if c is not None:
            c.set()

    def toolbar():
        try:
            return statusline.render(segments, state["sample"], cfg["model"], os.getcwd(), state["busy"])
        except Exception:
            return ""

    session = PromptSession(
        key_bindings=kb,
        bottom_toolbar=(toolbar if show_status else None),
        refresh_interval=(refresh if show_status else None),
    )

    def enqueue(text, images):
        if state["busy"]:
            print(f"queued ({work.qsize() + 1})", flush=True)
        work.put((text, images))

    with patch_stdout():
        while True:
            try:
                raw = session.prompt("> ")
            except (EOFError, KeyboardInterrupt):
                break
            line = raw.replace("[image]", "").strip()
            if pending_images:
                imgs = list(pending_images)
                pending_images.clear()
                enqueue(line or "What is in this image? Describe it.", imgs)
                continue
            if not line:
                continue
            if line.startswith("/"):
                action, ptext, imgs = _handle_command(line, cfg, messages, console, session_path)
                if action == "exit":
                    break
                if action == "run":
                    enqueue(ptext, imgs)
                elif action == "per_item":
                    if state["busy"]:
                        print(f"queued ({work.qsize() + 1})", flush=True)
                    work.put(ptext)
                continue
            enqueue(line, None)

    work.put(None)
    print("bye", flush=True)
    return 0


def _ensure_utf8_output() -> None:
    """Windows legacy consoles default to cp1252 and crash on emoji/box chars.
    Reconfigure the standard streams to UTF-8 so rich can render safely."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _ensure_utf8_output()
    parser = argparse.ArgumentParser(
        prog="gemma",
        description="LOCAL-Intelligence — a fully local CLI AI agent with filesystem, shell, document, web and vision tools.",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="'go' to start an interactive chat session (optionally 'go <folder>'), "
             "'update' to pull the latest source and reinstall, or 'skills' to list "
             "saved skills and their usage. "
             "Or pass a quoted question for a one-shot answer.",
    )
    parser.add_argument("-p", "--prompt", help="One-shot prompt; prints the answer and exits.")
    parser.add_argument("-i", "--image", action="append", help="Attach an image file (repeatable). Use with -p.")
    parser.add_argument("--model", help="Override the model (e.g. gemma4:12b, gemma4:e4b).")
    parser.add_argument("--num-ctx", type=int, help="Override context window size.")
    parser.add_argument("--searxng-url", help="Override the SearXNG base URL.")
    parser.add_argument("--no-thinking", action="store_true", help="Don't generate the model's reasoning at all — much faster on small GPUs; "
                             "slightly weaker on hard tasks. (Config: thinking: false.)")
    parser.add_argument("--verbose", action="store_true", help="Show full tool results, not previews.")
    parser.add_argument("--setup-config", action="store_true", help="Write a default config.yaml and exit.")
    parser.add_argument("--resume", "--continue", dest="resume", action="store_true",
                        help="Resume the most recent session in this folder.")
    parser.add_argument("--approve", choices=["none", "writes", "all"],
                        help="Require y/N approval before mutating actions (file writes/edits/deletes, shell).")
    parser.add_argument("--live", action="store_true",
                        help="Experimental live REPL: type-ahead queue, Esc-cancel, live status line.")
    parser.add_argument("--check", action="store_true",
                        help="With 'update': report whether newer commits exist, without installing.")
    parser.add_argument("--full", action="store_true",
                        help="With 'update': run the full installer (also checks Ollama, the model and SearXNG).")
    parser.add_argument("--repo", help="With 'update': path to the LOCAL-Intelligence source checkout.")
    parser.add_argument("--version", action="version", version=f"LOCAL-Intelligence {__version__}")
    args = parser.parse_args(argv)

    is_tty = sys.stdin.isatty()
    console = Console(force_terminal=True) if is_tty else Console()

    if args.setup_config:
        path = write_default_config()
        console.print(f"[green]Config written:[/green] {path}")
        return 0

    # `gemma update` — pull the latest source and reinstall, then exit.
    if args.command and args.command[0].lower() == "update":
        from .updater import update
        repo_arg = args.repo or (args.command[1] if len(args.command) > 1 else None)
        return update(load_config({}), console, repo_arg=repo_arg,
                      check_only=args.check, full=args.full)

    # `gemma skills` — report without starting a session.
    if args.command and args.command[0].lower() == "skills":
        if len(args.command) > 1:
            target = os.path.expanduser(args.command[1])
            if os.path.isdir(target):
                os.chdir(target)
        _skill_report(console)
        return 0

    # Interpret the positional command: `go [folder]` => interactive session;
    # any other bare text => treat as a one-shot prompt (like `gemma "question"`).
    repl_mode = args.prompt is None and args.image is None
    inline_prompt = args.prompt
    if args.command:
        if args.command[0].lower() == "go":
            repl_mode = True
            folder = args.command[1] if len(args.command) > 1 else None
            if folder:
                target = os.path.expanduser(folder)
                if not os.path.isdir(target):
                    console.print(f"[red]No such folder:[/red] {target}")
                    return 1
                os.chdir(target)
        elif args.prompt is None:
            inline_prompt = " ".join(args.command)
            repl_mode = False

    overrides = {
        "model": args.model,
        "num_ctx": args.num_ctx,
        "searxng_url": args.searxng_url,
    }
    if args.no_thinking:
        overrides["show_thinking"] = False
        overrides["thinking"] = False  # don't generate it, don't just hide it

    cfg = load_config(overrides)
    apply_to_tools(cfg)

    messages: List[Dict] = [{"role": "system", "content": build_system_prompt(cfg)}]
    renderer = Renderer(console, show_thinking=cfg.get("show_thinking", True), verbose=args.verbose)

    # Resume the latest session in this folder, or start a fresh one.
    session_path = None
    if args.resume:
        session_path = latest_session()
        if session_path:
            prior = load_session(session_path)
            if prior:
                messages.extend(prior)
                console.print(f"[dim]resumed {session_path.name} ({len(prior)} messages)[/dim]")
    if session_path is None:
        session_path = new_session_path()

    approve_mode = args.approve or cfg.get("approve", "none")
    approver = _make_approver(console) if approve_mode in ("writes", "all") else None

    if not repl_mode and inline_prompt:
        _run_once(cfg, messages, console, renderer, inline_prompt, images=args.image, approver=approver)
        save_session(session_path, messages, cfg["model"])
        return 0

    if args.image:
        console.print("[yellow]--image is only used with a one-shot prompt. Ignoring.[/yellow]")

    # Default: the simple, reliable line-by-line reader (renders answers cleanly).
    # The experimental live REPL (type-ahead queue / Esc-cancel / live status line)
    # is opt-in via --live or live_repl in config, and needs a real terminal and
    # no approval gate.
    want_live = args.live or cfg.get("live_repl", False)
    if want_live and is_tty and approver is None:
        return _repl_interactive(cfg, messages, console, renderer, session_path, approver=approver)
    return _repl_plain(cfg, messages, console, renderer, session_path, approver=approver)


if __name__ == "__main__":
    sys.exit(main())
