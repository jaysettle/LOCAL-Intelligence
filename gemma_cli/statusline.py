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
