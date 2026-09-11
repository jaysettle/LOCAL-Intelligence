#!/usr/bin/env python3
"""
Terminal rendering of the agent's event stream using rich.

Streams tokens live; renders thinking dim; shows each tool call as a compact
colored line with a short result preview (full result in --verbose).

Two rules, both learned from real terminals:

* Model text is printed with markup OFF. Rich reads `[...]` as markup, and a
  token that ends in a backslash escapes whatever tag follows it — so a Windows
  path streamed token-by-token, each wrapped in `[dim]...[/dim]`, came out as
  `C:[/dim]Users[/dim]jsettle`. Styling goes through the `style=` argument;
  anything model- or tool-derived that is interpolated into our own markup is
  passed through `escape()`.

* When a bottom-anchored widget shares the terminal (the status bar's rich
  Live, or prompt_toolkit's patch_stdout), only WHOLE lines may be printed
  above it: a flushed partial line lands on the widget's row and is redrawn on
  every refresh. `whole_lines=True` routes streamed text through a LineBuffer
  for exactly that reason. Output then arrives line by line instead of token
  by token — the price of rendering correctly under such a widget.
"""

from typing import Any, Dict, Iterator, List, Optional, Tuple

from rich.console import Console
from rich.markup import escape

Event = Tuple[str, Any]

_ARG_KEYS = ("path", "command", "pattern", "url", "query")


def _arg_preview(args: Dict[str, Any]) -> str:
    for k in _ARG_KEYS:
        if k in args and args[k]:
            v = str(args[k]).replace("\n", " ")
            return v[:80] + ("…" if len(v) > 80 else "")
    return ""


class LineBuffer:
    """Accumulate streamed text and hand back only complete lines.

    Emits at newlines, or soft-wraps at the last space once the pending text
    passes `width`, so a long paragraph still appears while it streams rather
    than all at once at the end.
    """

    def __init__(self, width: int = 100):
        self.width = max(20, int(width))
        self.pending = ""

    def push(self, text: str) -> List[str]:
        self.pending += text
        out: List[str] = []
        while True:
            nl = self.pending.find("\n")
            if nl != -1:
                out.append(self.pending[:nl])
                self.pending = self.pending[nl + 1:]
                continue
            if len(self.pending) >= self.width:
                cut = self.pending.rfind(" ", 0, self.width)
                if cut <= 0:
                    cut = self.width
                out.append(self.pending[:cut])
                self.pending = self.pending[cut:].lstrip(" ")
                continue
            return out

    def flush(self) -> Optional[str]:
        if not self.pending:
            return None
        line, self.pending = self.pending, ""
        return line


class Renderer:
    def __init__(self, console: Console, show_thinking: bool = True, verbose: bool = False,
                 whole_lines: bool = False):
        self.console = console
        self.show_thinking = show_thinking
        self.verbose = verbose
        # Set (temporarily) by the caller while a bottom-anchored widget is up.
        self.whole_lines = whole_lines

    def _line_width(self) -> int:
        try:
            return max(20, int(self.console.width) - 1)
        except Exception:
            return 100

    def consume(self, events: Iterator[Event]) -> str:
        """Render the event stream. Returns the final assistant text."""
        answer_parts: List[str] = []
        mode: Optional[str] = None  # None | "think" | "text"
        buf = LineBuffer(self._line_width()) if self.whole_lines else None

        def emit(text: str, style: Optional[str] = None, end: str = "\n") -> None:
            # markup=False: model text is data, never markup (see module docstring).
            self.console.print(text, style=style, end=end, soft_wrap=True, markup=False, highlight=False)

        def stream(text: str, style: Optional[str]) -> None:
            if buf is None:
                emit(text, style, end="")
            else:
                for line in buf.push(text):
                    emit(line, style)

        def close_stream() -> None:
            nonlocal mode
            if buf is not None:
                rest = buf.flush()
                if rest is not None:
                    emit(rest, "dim" if mode == "think" else None)
            elif mode is not None:
                self.console.print()
            mode = None

        for kind, payload in events:
            if kind == "think":
                if not self.show_thinking:
                    continue
                if mode != "think":
                    close_stream()
                    self.console.print("[dim italic]thinking…[/dim italic]", end=("\n" if buf else ""))
                    mode = "think"
                stream(payload, "dim")

            elif kind == "text":
                if mode != "text":
                    close_stream()
                    if buf is None:
                        self.console.print()  # separate the answer from tool lines above
                    mode = "text"
                answer_parts.append(payload)
                stream(payload, None)

            elif kind == "tool_start":
                close_stream()
                name = escape(str(payload.get("name", "")))
                preview = escape(_arg_preview(payload.get("args", {}) or {}))
                self.console.print(f"[bold cyan]🔧 {name}[/bold cyan] [dim]{preview}[/dim]")

            elif kind == "tool_result":
                result = payload.get("result", "") or ""
                if self.verbose:
                    self.console.print(escape(result), style="dim")
                else:
                    first = result.strip().splitlines()[0] if result.strip() else "(no output)"
                    more = result.count("\n")
                    tail = f" [dim](+{more} lines)[/dim]" if more else ""
                    self.console.print(f"   [dim]{escape(first[:120])}[/dim]{tail}")

            elif kind == "notice":
                close_stream()
                self.console.print(f"[dim italic]· {escape(str(payload))}[/dim italic]")

            elif kind == "error":
                close_stream()
                self.console.print(f"[bold red]Error:[/bold red] {escape(str(payload))}")

            elif kind == "child":
                # A child run's own events, shown indented and compact: its tool
                # activity and errors, not its streamed text (the result line
                # arrives as child_result). --verbose shows the text too.
                ck, cp = payload
                if ck == "tool_start":
                    close_stream()
                    name = escape(str((cp or {}).get("name", "")))
                    preview = escape(_arg_preview((cp or {}).get("args", {}) or {}))
                    self.console.print(f"    [dim]↳[/dim] [cyan]{name}[/cyan] [dim]{preview}[/dim]")
                elif ck == "error":
                    close_stream()
                    self.console.print(f"    [dim]↳[/dim] [red]Error:[/red] {escape(str(cp))}")
                elif ck == "notice":
                    # Loop detection, compaction, "stopped" - a child's harness
                    # events were invisible before; a loop looked like paging.
                    close_stream()
                    self.console.print(f"    [dim]↳ · {escape(str(cp))}[/dim]")
                elif ck == "text" and self.verbose:
                    stream(str(cp), "dim")

            elif kind == "child_result":
                close_stream()
                res = (payload.get("result") or "").strip()
                first = res.splitlines()[0] if res else "(no result)"
                label = escape(str(payload.get("label", "")))
                calls = payload.get("calls")
                tail = f"  [dim]({calls} tool calls)[/dim]" if calls and calls > 1 else ""
                self.console.print(f"  [dim]↳ {label}:[/dim] {escape(first[:110])}{tail}")

            elif kind == "done":
                close_stream()

        close_stream()
        if answer_parts and buf is None:
            self.console.print()  # trailing newline after a token-streamed answer
        return "".join(answer_parts).strip()
