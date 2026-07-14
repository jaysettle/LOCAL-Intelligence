#!/usr/bin/env python3
"""
Terminal rendering of the agent's event stream using rich.

Streams tokens live; renders thinking dim; shows each tool call as a compact
colored line with a short result preview (full result in --verbose). At the end
of a turn the full answer is re-rendered as markdown for clean formatting.
"""

from typing import Any, Dict, Iterator, List, Tuple

from rich.console import Console

Event = Tuple[str, Any]

_ARG_KEYS = ("path", "command", "pattern", "url", "query")


def _arg_preview(args: Dict[str, Any]) -> str:
    for k in _ARG_KEYS:
        if k in args and args[k]:
            v = str(args[k]).replace("\n", " ")
            return v[:80] + ("…" if len(v) > 80 else "")
    return ""


class Renderer:
    def __init__(self, console: Console, show_thinking: bool = True, verbose: bool = False):
        self.console = console
        self.show_thinking = show_thinking
        self.verbose = verbose

    def consume(self, events: Iterator[Event]) -> str:
        """Render the event stream. Returns the final assistant text."""
        answer_parts: List[str] = []
        mode = None  # None | "think" | "text"

        for kind, payload in events:
            if kind == "think":
                if not self.show_thinking:
                    continue
                if mode != "think":
                    self.console.print("\n[dim italic]thinking…[/dim italic]", end="")
                    mode = "think"
                self.console.print(f"[dim]{payload}[/dim]", end="", soft_wrap=True)

            elif kind == "text":
                if mode == "think":
                    self.console.print()  # close thinking block
                if mode != "text":
                    self.console.print()  # separate answer from any tool lines above
                mode = "text"
                answer_parts.append(payload)
                self.console.print(payload, end="", soft_wrap=True)

            elif kind == "tool_start":
                if mode is not None:
                    self.console.print()
                mode = None
                name = payload["name"]
                self.console.print(f"[bold cyan]🔧 {name}[/bold cyan] [dim]{_arg_preview(payload['args'])}[/dim]")

            elif kind == "tool_result":
                result = payload["result"]
                if self.verbose:
                    self.console.print(f"[dim]{result}[/dim]")
                else:
                    first = result.strip().splitlines()[0] if result.strip() else "(no output)"
                    more = result.count("\n")
                    tail = f" [dim](+{more} lines)[/dim]" if more else ""
                    self.console.print(f"   [dim]{first[:120]}[/dim]{tail}")

            elif kind == "notice":
                if mode is not None:
                    self.console.print()
                mode = None
                self.console.print(f"[dim italic]· {payload}[/dim italic]")

            elif kind == "error":
                if mode is not None:
                    self.console.print()
                mode = None
                self.console.print(f"[bold red]Error:[/bold red] {payload}")

            elif kind == "done":
                if mode is not None:
                    self.console.print()
                mode = None

        if answer_parts:
            self.console.print()  # trailing newline after the streamed answer
        return "".join(answer_parts).strip()
