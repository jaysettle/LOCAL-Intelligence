#!/usr/bin/env python3
"""
Configuration: layered defaults -> config.yaml -> environment -> CLI flags.
Also applies runtime settings into the tool modules (allowed write roots, SearXNG URL).
"""

import os
import platform
from pathlib import Path
from typing import Any, Dict, List

import yaml

_IS_WINDOWS = platform.system() == "Windows"


def config_dir() -> Path:
    if _IS_WINDOWS:
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "gemma-cli"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "gemma-cli"


CONFIG_PATH = config_dir() / "config.yaml"

DEFAULTS: Dict[str, Any] = {
    "model": "gemma4:12b",
    "num_ctx": 32768,
    "ollama_url": "http://localhost:11434",
    "searxng_url": "http://localhost:8899",
    "keep_alive": "30m",
    "max_tool_iterations": 25,
    "show_thinking": True,
    # None => defaults to [home, tempdir, cwd] at load time
    "allowed_write_roots": None,
    "timeout": 600,
    # Memory
    "project_memory_file": "GEMMA.md",
    "global_memory_file": None,  # None => <config_dir>/memory.md
    # Reliability
    "compact_at_ratio": 0.75,   # summarize old turns past this fraction of num_ctx
    # Safety: none | writes | all  (which tool categories need y/n approval)
    "approve": "none",
    # Optional smaller model for internal utility calls (compaction, titles)
    "fast_model": None,
    # Interactive status line (below the input): shows folder + live GPU/CPU/VRAM
    "status_line": True,
    "status_segments": ["folder", "gpu", "vram", "cpu", "model"],
    "status_refresh": 0.5,
    # Experimental live REPL (type-ahead queue + Esc-cancel + live status line).
    # Off by default: the simple line-by-line reader is the reliable default.
    "live_repl": False,
}

_ENV_MAP = {
    "GEMMA_MODEL": ("model", str),
    "GEMMA_NUM_CTX": ("num_ctx", int),
    "GEMMA_OLLAMA_URL": ("ollama_url", str),
    "GEMMA_SEARXNG_URL": ("searxng_url", str),
    "GEMMA_KEEP_ALIVE": ("keep_alive", str),
    "GEMMA_MAX_TOOL_ITERATIONS": ("max_tool_iterations", int),
}


def default_write_roots() -> List[str]:
    import tempfile
    return [str(Path.home()), tempfile.gettempdir(), os.getcwd()]


def load_config(overrides: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)

    # Layer 1: config.yaml
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f) or {}
            cfg.update({k: v for k, v in file_cfg.items() if v is not None})
        except Exception as e:
            print(f"Warning: could not read {CONFIG_PATH}: {e}")

    # Layer 2: environment
    for env_key, (cfg_key, caster) in _ENV_MAP.items():
        if env_key in os.environ:
            try:
                cfg[cfg_key] = caster(os.environ[env_key])
            except ValueError:
                pass

    # Layer 3: explicit CLI overrides (only non-None)
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})

    if not cfg.get("allowed_write_roots"):
        cfg["allowed_write_roots"] = default_write_roots()

    # Always allow writing in the folder gemma was launched from (project cwd),
    # so `gemma go` in a project directory can edit its files like a dev CLI.
    cwd = os.getcwd()
    if cwd not in cfg["allowed_write_roots"]:
        cfg["allowed_write_roots"] = list(cfg["allowed_write_roots"]) + [cwd]

    # Resolve the default global memory path if not set explicitly.
    if not cfg.get("global_memory_file"):
        cfg["global_memory_file"] = str(config_dir() / "memory.md")

    return cfg


def write_default_config() -> Path:
    """Write a starter config.yaml if none exists. Returns the path."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        return CONFIG_PATH
    starter = dict(DEFAULTS)
    starter["allowed_write_roots"] = default_write_roots()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("# LOCAL-Intelligence (gemma) configuration\n")
        f.write("# Edit values below; env vars (GEMMA_MODEL, GEMMA_NUM_CTX, ...) override these.\n\n")
        yaml.safe_dump(starter, f, sort_keys=False, default_flow_style=False)
    return CONFIG_PATH


def apply_to_tools(cfg: Dict[str, Any]) -> None:
    """Push runtime config into the tool modules."""
    from .tools import file_tools, web_tools, memory_tools
    file_tools.set_allowed_write_roots([Path(p) for p in cfg["allowed_write_roots"]])
    web_tools.set_searxng_url(cfg["searxng_url"])
    memory_tools.configure(cfg.get("project_memory_file", "GEMMA.md"), cfg.get("global_memory_file", ""))
