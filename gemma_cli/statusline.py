#!/usr/bin/env python3
"""
Status line for the interactive REPL: working folder + GPU/CPU/VRAM.

Sampled only while a response is processing (per the design), so nothing polls
at rest. GPU comes from nvidia-smi (absent -> n/a); CPU from psutil.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional

_NVIDIA = shutil.which("nvidia-smi")

try:
    import psutil
    psutil.cpu_percent(interval=None)  # prime; first real read won't be 0.0
    _HAVE_PSUTIL = True
except Exception:
    _HAVE_PSUTIL = False


@dataclass
class Sample:
    gpu_pct: Optional[int] = None
    vram_used: Optional[int] = None   # MB
    vram_total: Optional[int] = None  # MB
    cpu_pct: Optional[float] = None


def _gpu():
    if not _NVIDIA:
        return (None, None, None)
    try:
        out = subprocess.run(
            [_NVIDIA, "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=1.5,
        )
        line = (out.stdout or "").strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return (None, None, None)


def sample() -> Sample:
    g, vu, vt = _gpu()
    cpu = None
    if _HAVE_PSUTIL:
        try:
            cpu = psutil.cpu_percent(interval=None)
        except Exception:
            cpu = None
    return Sample(g, vu, vt, cpu)


def render(segments: List[str], smp: Optional[Sample], model: str, cwd: str, sampling: bool) -> str:
    """Build the toolbar string from the configured segments.

    When `sampling` is False (idle) only the static segments (folder/model) show,
    plus an 'idle' marker; live metrics appear only while processing.
    """
    parts: List[str] = []
    for seg in segments:
        if seg == "folder":
            base = os.path.basename(cwd.rstrip("/\\")) or cwd
            parts.append(f"dir:{base}")
        elif seg == "model":
            parts.append(model)
        elif seg == "gpu" and sampling:
            parts.append(f"GPU {smp.gpu_pct}%" if smp and smp.gpu_pct is not None else "GPU n/a")
        elif seg == "vram" and sampling:
            if smp and smp.vram_total:
                parts.append(f"VRAM {smp.vram_used / 1024:.1f}/{smp.vram_total / 1024:.1f}G")
        elif seg == "cpu" and sampling:
            if smp and smp.cpu_pct is not None:
                parts.append(f"CPU {smp.cpu_pct:.0f}%")
    if not sampling:
        parts.append("idle")
    return "   ".join(p for p in parts if p)


def _stdout_is_tty() -> bool:
    """Is stdout really a terminal? (Module-level so tests can monkeypatch it.)"""
    import sys
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


class StatusBar:
    """Bottom status line for the plain REPL and one-shot runs.

    The --live REPL gets its bar from prompt_toolkit's bottom_toolbar, which
    only exists while a prompt is active. The plain REPL has no prompt while the
    model is generating — precisely when the numbers are interesting — so this
    uses a rich Live anchored at the bottom instead: everything the Renderer
    prints scrolls above it. Sampling runs on a background thread only while the
    bar is up, so nothing polls at rest.

    Contract: whoever prints while this is active must print WHOLE lines
    (Renderer(whole_lines=True)). A flushed partial line lands on the bar's row.

    Use as a context manager. Does nothing when the console is not a terminal or
    `status_line` is off, so callers can always wrap unconditionally.
    """

    def __init__(self, console, cfg):
        self.console = console
        self.segments = list(cfg.get("status_segments") or ["folder", "gpu", "vram", "cpu", "model"])
        self.model = str(cfg.get("model", ""))
        self.refresh = float(cfg.get("status_refresh", 0.5) or 0.5)
        # console.is_terminal is not enough: main.py forces it whenever STDIN is a
        # tty, so `gemma -p "..." > report.txt` still has a "terminal" console —
        # and every refresh frame would land in the file. Require a real stdout.
        self.enabled = (
            bool(cfg.get("status_line", True))
            and bool(getattr(console, "is_terminal", False))
            and _stdout_is_tty()
        )
        self._live = None
        self._stop = None
        self._thread = None
        self._sample: Optional[Sample] = None

    def _renderable(self):
        from rich.text import Text
        try:
            line = render(self.segments, self._sample, self.model, os.getcwd(), sampling=True)
        except Exception:
            line = ""
        return Text(line, style="reverse")

    def __enter__(self):
        if not self.enabled:
            return self
        import threading
        from rich.live import Live

        self._stop = threading.Event()
        self._sample = sample()
        self._live = Live(
            self._renderable(),
            console=self.console,
            transient=True,                       # the bar disappears when the turn ends
            refresh_per_second=max(1.0, 1.0 / self.refresh),
        )
        self._live.start()

        def loop():
            while not self._stop.wait(self.refresh):
                self._sample = sample()
                try:
                    self._live.update(self._renderable())
                except Exception:
                    pass

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
        self._live = None
        self._thread = None
        return False
