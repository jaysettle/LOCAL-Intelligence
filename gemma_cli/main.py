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

    while True:
        try:
            line = console.input("[bold green]›[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            return 0

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
                    "[dim]/clear — reset conversation\n"
                    "/model <tag> — switch model\n"
                    "/image <path> <prompt> — attach an image\n"
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
