"""Tests for the status line and cooperative turn cancellation."""
import threading

from gemma_cli import statusline
from gemma_cli.statusline import Sample, render


# --- status line rendering ----------------------------------------------

def test_render_all_segments_while_sampling():
    smp = Sample(gpu_pct=45, vram_used=6144, vram_total=8192, cpu_pct=22.4)
    out = render(["folder", "gpu", "vram", "cpu", "model"], smp, "gemma4:12b",
                 "/home/x/myproj", sampling=True)
    assert "dir:myproj" in out
    assert "GPU 45%" in out
    assert "VRAM 6.0/8.0G" in out
    assert "CPU 22%" in out
    assert "gemma4:12b" in out


def test_render_idle_hides_metrics():
    out = render(["folder", "gpu", "cpu", "model"], None, "m", "/a/b/proj", sampling=False)
    assert "dir:proj" in out
    assert "idle" in out
    assert "GPU" not in out
    assert "CPU" not in out


def test_render_gpu_na_when_unavailable():
    smp = Sample(gpu_pct=None, cpu_pct=10.0)
    out = render(["gpu"], smp, "m", "/x", sampling=True)
    assert "GPU n/a" in out


def test_gpu_parse(monkeypatch):
    import subprocess

    class R:
        stdout = "45, 6100, 8188\n"

    monkeypatch.setattr(statusline, "_NVIDIA", "nvidia-smi")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    assert statusline._gpu() == (45, 6100, 8188)


def test_gpu_none_without_nvidia(monkeypatch):
    monkeypatch.setattr(statusline, "_NVIDIA", None)
    assert statusline._gpu() == (None, None, None)


# --- cooperative cancellation -------------------------------------------

def test_run_turn_cancel_stops_early(monkeypatch):
    from gemma_cli import agent

    class FakeResp:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield b'{"message":{"content":"hi"},"done":false}'
            yield b'{"message":{"content":" there"},"done":true}'

        def close(self):
            pass

    monkeypatch.setattr(agent.requests, "post", lambda *a, **k: FakeResp())

    cancel = threading.Event()
    cancel.set()  # already cancelled before the first streamed line
    cfg = {"ollama_url": "x", "model": "m", "num_ctx": 100, "max_tool_iterations": 5, "timeout": 5}
    messages = [{"role": "system", "content": "s"}]
    events = list(agent.run_turn(cfg, messages, "hello", cancel=cancel))

    assert any(k == "notice" and v == "stopped" for k, v in events)
    assert any(k == "done" for k, _ in events)
    # It must NOT have streamed the full answer after cancellation.
    text = "".join(v for k, v in events if k == "text")
    assert "there" not in text
