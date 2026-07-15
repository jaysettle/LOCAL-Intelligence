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
  gemma --setup-config          write a default config.yaml and exit

Interactive REPL: input stays live while it answers; type + Enter to queue the
next prompt; Esc stops the current answer; Alt+V pastes a clipboard image; a
status line under the input shows the folder + GPU/CPU/VRAM while it works.
REPL commands: /paste /image /clear /model /save /sessions /resume /help /exit
"""

import argparse
import os
import sys
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


def _run_once(cfg, messages, console, renderer, text, images=None, approver=None) -> None:
    events = run_turn(cfg, messages, text, image_paths=images, approver=approver)
    renderer.consume(events)


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
    "/exit — quit    (while answering: Esc stops it, typing queues the next)[/dim]"
)


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

    console.print(f"[red]unknown command: {cmd}[/red] [dim](/help)[/dim]")
    return ("handled", None, None)


def _banner(cfg, console) -> None:
    console.print(f"[bold]LOCAL-Intelligence[/bold] [dim]v{__version__}[/dim] — model [cyan]{cfg['model']}[/cyan]")
    console.print(f"[dim]working dir:[/dim] [green]{os.getcwd()}[/green]")


# ---------------------------------------------------------------------------
# Plain REPL (non-interactive stdin, or when approval prompts are active)
# ---------------------------------------------------------------------------

def _repl_plain(cfg, messages, console, renderer, session_path, approver=None) -> int:
    _banner(cfg, console)
    console.print("[dim]Type your message. /help for commands, /exit to quit.[/dim]\n")
    _preflight(cfg, console)

    while True:
        try:
            line = console.input("[bold green]>[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
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
            continue
        _run_once(cfg, messages, console, renderer, line, approver=approver)
        save_session(session_path, messages, cfg["model"])


# ---------------------------------------------------------------------------
# Interactive REPL (live input, type-ahead queue, Esc-cancel, status line)
# ---------------------------------------------------------------------------

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
            text, images = item
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
                events = run_turn(cfg, messages, text, image_paths=images, approver=approver, cancel=cancel)
                renderer.consume(events)
            except Exception as e:
                console.print(f"[red]error: {e}[/red]")
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
            console.print(f"[dim]queued ({work.qsize() + 1})[/dim]")
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
                continue
            enqueue(line, None)

    work.put(None)
    console.print("[dim]bye[/dim]")
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
        description="LOCAL-Intelligence — a fully local CLI AI agent with filesystem, shell, web and vision tools.",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="'go' to start an interactive chat session (optionally 'go <folder>'). "
             "Or pass a quoted question for a one-shot answer.",
    )
    parser.add_argument("-p", "--prompt", help="One-shot prompt; prints the answer and exits.")
    parser.add_argument("-i", "--image", action="append", help="Attach an image file (repeatable). Use with -p.")
    parser.add_argument("--model", help="Override the model (e.g. gemma4:12b, gemma4:e4b).")
    parser.add_argument("--num-ctx", type=int, help="Override context window size.")
    parser.add_argument("--searxng-url", help="Override the SearXNG base URL.")
    parser.add_argument("--no-thinking", action="store_true", help="Hide the model's reasoning output.")
    parser.add_argument("--verbose", action="store_true", help="Show full tool results, not previews.")
    parser.add_argument("--setup-config", action="store_true", help="Write a default config.yaml and exit.")
    parser.add_argument("--resume", "--continue", dest="resume", action="store_true",
                        help="Resume the most recent session in this folder.")
    parser.add_argument("--approve", choices=["none", "writes", "all"],
                        help="Require y/N approval before mutating actions (file writes/edits/deletes, shell).")
    parser.add_argument("--version", action="version", version=f"LOCAL-Intelligence {__version__}")
    args = parser.parse_args(argv)

    is_tty = sys.stdin.isatty()
    console = Console(force_terminal=True) if is_tty else Console()

    if args.setup_config:
        path = write_default_config()
        console.print(f"[green]Config written:[/green] {path}")
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

    # Interactive live REPL when we own a real terminal and there's no approval
    # gate (approval prompts need synchronous stdin, which the live loop can't
    # share). Otherwise the plain reader.
    if is_tty and approver is None:
        return _repl_interactive(cfg, messages, console, renderer, session_path, approver=approver)
    return _repl_plain(cfg, messages, console, renderer, session_path, approver=approver)


if __name__ == "__main__":
    sys.exit(main())
