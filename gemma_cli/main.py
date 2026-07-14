#!/usr/bin/env python3
"""
LOCAL-Intelligence CLI entry point.

  gemma                         interactive REPL
  gemma -p "prompt"             one-shot, prints answer and exits
  gemma -i img.png -p "..."     attach an image (vision)
  gemma --model gemma4:e4b      override model for this run
  gemma --setup-config          write a default config.yaml and exit

REPL commands: /clear  /model <tag>  /image <path> <prompt>  /help  /exit
"""

import argparse
import sys
from typing import Dict, List, Optional

from rich.console import Console

from . import __version__
from .agent import run_turn
from .config import load_config, write_default_config, apply_to_tools, CONFIG_PATH
from .render import Renderer
from .sysprompt import build_system_prompt


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


def _run_once(cfg, messages, console, renderer, text, images=None) -> None:
    events = run_turn(cfg, messages, text, image_paths=images)
    renderer.consume(events)


def _repl(cfg: Dict, messages: List[Dict], console: Console, renderer: Renderer) -> int:
    console.print(f"[bold]LOCAL-Intelligence[/bold] [dim]v{__version__}[/dim] — model [cyan]{cfg['model']}[/cyan]")
    console.print("[dim]Type your message. /help for commands, /exit to quit.[/dim]\n")
    _preflight(cfg, console)

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
            if cmd == "/image":
                if len(parts) < 3:
                    console.print("[red]usage: /image <path> <prompt>[/red]")
                    continue
                _run_once(cfg, messages, console, renderer, parts[2], images=[parts[1]])
                continue
            console.print(f"[red]unknown command: {cmd}[/red] [dim](/help)[/dim]")
            continue

        _run_once(cfg, messages, console, renderer, line)


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
    parser.add_argument("-p", "--prompt", help="One-shot prompt; prints the answer and exits.")
    parser.add_argument("-i", "--image", action="append", help="Attach an image file (repeatable). Use with -p.")
    parser.add_argument("--model", help="Override the model (e.g. gemma4:12b, gemma4:e4b).")
    parser.add_argument("--num-ctx", type=int, help="Override context window size.")
    parser.add_argument("--searxng-url", help="Override the SearXNG base URL.")
    parser.add_argument("--no-thinking", action="store_true", help="Hide the model's reasoning output.")
    parser.add_argument("--verbose", action="store_true", help="Show full tool results, not previews.")
    parser.add_argument("--setup-config", action="store_true", help="Write a default config.yaml and exit.")
    parser.add_argument("--version", action="version", version=f"LOCAL-Intelligence {__version__}")
    args = parser.parse_args(argv)

    console = Console()

    if args.setup_config:
        path = write_default_config()
        console.print(f"[green]Config written:[/green] {path}")
        return 0

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

    if args.prompt:
        _run_once(cfg, messages, console, renderer, args.prompt, images=args.image)
        return 0

    if args.image:
        console.print("[yellow]--image is only used with -p/--prompt. Ignoring.[/yellow]")

    return _repl(cfg, messages, console, renderer)


if __name__ == "__main__":
    sys.exit(main())
