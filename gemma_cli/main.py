#!/usr/bin/env python3
"""
LOCAL-Intelligence CLI entry point.

  gemma go                      interactive chat session in the current folder
  gemma go C:\path\to\project   interactive chat session in a specific folder
  gemma                         interactive chat session (same as `go`)
  gemma "quick question"        one-shot, prints answer and exits
  gemma -p "prompt"             one-shot (explicit flag form)
  gemma -i img.png -p "..."     attach an image (vision)
  gemma --model gemma4:e4b      override model for this run
  gemma --setup-config          write a default config.yaml and exit

REPL commands: /clear  /model <tag>  /image <path> <prompt>  /help  /exit
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


def _repl(cfg: Dict, messages: List[Dict], console: Console, renderer: Renderer, session_path, approver=None) -> int:
    console.print(f"[bold]LOCAL-Intelligence[/bold] [dim]v{__version__}[/dim] — model [cyan]{cfg['model']}[/cyan]")
    console.print(f"[dim]working dir:[/dim] [green]{os.getcwd()}[/green]")
    console.print("[dim]Type your message. /help for commands, /exit to quit.[/dim]\n")
    _preflight(cfg, console)

    def persist():
        save_session(session_path, messages, cfg["model"])

    pending_images: List[str] = []
    prompt_session = _make_prompt_session(pending_images)
    if prompt_session is not None:
        console.print("[dim]Tip: press Alt+V to attach an image from your clipboard (Win+Shift+S to snip).[/dim]\n")

    def read_line() -> str:
        if prompt_session is not None:
            return prompt_session.prompt("> ")
        return console.input("[bold green]›[/bold green] ")

    while True:
        try:
            raw = read_line()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return 0

        # An Alt+V paste inserts a "[image]" marker and queues the file.
        line = raw.replace("[image]", "").strip()
        if pending_images:
            imgs = list(pending_images)
            pending_images.clear()
            _run_once(cfg, messages, console, renderer,
                      line or "What is in this image? Describe it.", images=imgs, approver=approver)
            persist()
            continue

        if not line:
            continue

        if line.startswith("/"):
            parts = line.split(maxsplit=2)
            cmd = parts[0].lower()
            if cmd in ("/exit", "/quit"):
                console.print("[dim]bye[/dim]")
                return 0
            if cmd == "/help":
                console.print(
                    "[dim]/paste [prompt] — attach the image on your clipboard (Win+Shift+S to snip)\n"
                    "/image <path> <prompt> — attach an image file\n"
                    "/clear — reset conversation\n"
                    "/model <tag> — switch model\n"
                    "/save — save this session now\n"
                    "/sessions — list saved sessions in this folder\n"
                    "/resume [name] — load the latest (or named) session\n"
                    "/exit — quit[/dim]"
                )
                continue
            if cmd == "/clear":
                messages[:] = [messages[0]]  # keep system prompt
                console.print("[dim]conversation cleared[/dim]")
                continue
            if cmd == "/model":
                if len(parts) < 2:
                    console.print(f"[dim]current model: {cfg['model']}[/dim]")
                else:
                    cfg["model"] = parts[1]
                    console.print(f"[dim]model set to {cfg['model']}[/dim]")
                continue
            if cmd == "/save":
                persist()
                console.print(f"[dim]saved {session_path.name}[/dim]")
                continue
            if cmd == "/sessions":
                sessions = list_sessions()
                if not sessions:
                    console.print("[dim]no saved sessions in this folder[/dim]")
                else:
                    for p, meta in sessions[:15]:
                        console.print(f"[dim]{summarize(p, meta)}[/dim]")
                continue
            if cmd == "/resume":
                if len(parts) >= 2:
                    target = session_dir_lookup(parts[1])
                else:
                    target = latest_session()
                if not target:
                    console.print("[red]no matching session[/red]")
                    continue
                prior = load_session(target)
                messages[:] = [messages[0]] + prior
                console.print(f"[dim]resumed {target.name} ({len(prior)} messages)[/dim]")
                continue
            if cmd == "/image":
                if len(parts) < 3:
                    console.print("[red]usage: /image <path> <prompt>[/red]")
                    continue
                _run_once(cfg, messages, console, renderer, parts[2], images=[parts[1]], approver=approver)
                persist()
                continue
            if cmd == "/paste":
                rest = line.split(maxsplit=1)
                prompt_text = rest[1] if len(rest) > 1 else "What is in this image? Describe it."
                path, err = _grab_clipboard_to_file()
                if not path:
                    console.print(f"[yellow]{err}. Snip with Win+Shift+S or copy an image, then retry.[/yellow]")
                    continue
                _run_once(cfg, messages, console, renderer, prompt_text, images=[path], approver=approver)
                persist()
                continue
            console.print(f"[red]unknown command: {cmd}[/red] [dim](/help)[/dim]")
            continue

        _run_once(cfg, messages, console, renderer, line, approver=approver)
        persist()


def session_dir_lookup(name: str):
    """Resolve a session name/filename to a path in this folder's session dir."""
    from .sessions import session_dir
    d = session_dir()
    for cand in (d / name, d / f"{name}.json"):
        if cand.exists():
            return cand


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
    # Copied image file(s) from Explorer -> list of paths.
    if isinstance(data, list):
        imgs = [p for p in data if str(p).lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))]
        return (imgs[0], None) if imgs else (None, "clipboard has files but no image")
    # A bitmap (snip / copied from an app) -> PIL Image; save to a temp PNG.
    try:
        import os
        import tempfile
        fd, tmp = tempfile.mkstemp(prefix="gemma_paste_", suffix=".png")
        os.close(fd)
        data.save(tmp, "PNG")
        return tmp, None
    except Exception as e:
        return None, f"could not save pasted image: {e}"


def _make_prompt_session(pending_images: List[str]):
    """A prompt_toolkit session with Alt+V bound to paste a clipboard image.

    Returns None when input isn't an interactive TTY (piped/scripted) or
    prompt_toolkit isn't available — callers fall back to a plain reader.
    """
    if not sys.stdin.isatty():
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return None

    kb = KeyBindings()

    @kb.add("escape", "v")  # Alt+V (Meta+V is sent as ESC then v)
    def _paste(event):
        path, _err = _grab_clipboard_to_file()
        if path:
            pending_images.append(path)
            event.app.current_buffer.insert_text("[image] ")
        else:
            event.app.output.bell()

    return PromptSession(key_bindings=kb)
    return None


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

    console = Console()

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

    return _repl(cfg, messages, console, renderer, session_path, approver=approver)


if __name__ == "__main__":
    sys.exit(main())
