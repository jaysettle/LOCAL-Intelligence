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
    # 127.0.0.1, not localhost: on Windows "localhost" tries IPv6 (::1) first,
    # Ollama listens on IPv4 only, and every new connection then waits ~2 s for
    # the refusal before falling back (measured: 2.05 s vs 0.01 s per request).
    "ollama_url": "http://127.0.0.1:11434",
    "searxng_url": "http://localhost:8899",
    "keep_alive": "30m",
    "max_tool_iterations": 25,
    "show_thinking": True,
    # Generate the model's reasoning at all. Measured on an RTX 2070 with
    # gemma4:12b: a trivial turn took 67s with thinking and 10s without.
    # --no-thinking sets this False (it used to only HIDE the reasoning).
    "thinking": True,
    # Child runs (a skill in per-file mode runs one child per file): thinking is
    # off for them by default because their work is mechanical and they run N
    # times; a smaller tool budget and a capped result keep them bounded.
    "child_thinking": False,
    "child_max_tool_iterations": 10,
    "child_result_chars": 2000,
    # Children get no write_file/edit_file/delete_file/shell unless a skill sets
    # child_writes: true. The final synthesis turn is where files get written.
    "child_readonly": True,
    "per_file_max_items": 12,
    # Loop detection: the same tool call a third time gets a warning instead of
    # running. After this many warnings in one turn, the turn ends - observed
    # live, warnings alone never stopped a stuck model and it burned all 25 calls.
    "max_loop_nudges": 2,
    # None => defaults to [home, tempdir, cwd] at load time
    "allowed_write_roots": None,
    "timeout": 600,
    # Memory
    "project_memory_file": "GEMMA.md",
    "global_memory_file": None,  # None => <config_dir>/memory.md
    # Reliability
    "compact_at_ratio": 0.75,   # summarize old turns past this fraction of num_ctx
    # Where the source checkout lives, for `gemma update`. Filled in automatically
    # the first time an update runs.
    "repo_dir": None,
    # Skills: let the model load a saved skill on its own via the load_skill tool.
    # Turn off if a small model over-triggers; /<name> still works either way.
    "allow_model_skills": True,
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
    # Fast typed decisions (see decide.py). decide_thinking: off | shadow | on.
    # shadow asks "does this prompt need step-by-step reasoning?" before each turn
    # and logs the answer with the turn's duration to .gemma/decisions.jsonl,
    # changing nothing; on turns thinking off for prompts confidently judged
    # simple (confidence >= decide_threshold). /decisions shows the log.
    "decide_thinking": "off",
    "decide_threshold": 0.8,
    # gemma = the local model through Ollama (private, ~0.5 s per question).
    # systemone = a server speaking TypeSafe's System One API, e.g. Jev at
    # https://api.typesafe.ai - the prompt text then LEAVES THIS MACHINE.
    "decide_backend": "gemma",
    "decide_url": "https://api.typesafe.ai",
    "decide_model": None,          # None: the chat model (gemma) or jev-latest (systemone)
    "decide_api_key_env": "TYPESAFE_API_KEY",   # the key is read from this env var, never stored
    "decide_timeout": 60,
}

def _as_bool(value: str) -> bool:
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


_ENV_MAP = {
    "GEMMA_MODEL": ("model", str),
    "GEMMA_NUM_CTX": ("num_ctx", int),
    "GEMMA_OLLAMA_URL": ("ollama_url", str),
    "GEMMA_SEARXNG_URL": ("searxng_url", str),
    "GEMMA_KEEP_ALIVE": ("keep_alive", str),
    "GEMMA_MAX_TOOL_ITERATIONS": ("max_tool_iterations", int),
    "GEMMA_THINKING": ("thinking", _as_bool),
    "GEMMA_DECIDE_THINKING": ("decide_thinking", str),
    "GEMMA_DECIDE_BACKEND": ("decide_backend", str),
    "GEMMA_DECIDE_URL": ("decide_url", str),
}


def _loopback_ipv4(url: str) -> str:
    """http://localhost:11434 -> http://127.0.0.1:11434; anything else unchanged."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if (parts.hostname or "").lower() != "localhost":
        return url
    netloc = "127.0.0.1" + (f":{parts.port}" if parts.port else "")
    if parts.username:
        netloc = f"{parts.username}{':' + parts.password if parts.password else ''}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


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

    # Starter config files copied every default, so many say localhost. Same
    # machine, same port - minus the ~2 s IPv6 detour on every request (above).
    cfg["ollama_url"] = _loopback_ipv4(str(cfg.get("ollama_url") or DEFAULTS["ollama_url"]))

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
    from .tools import file_tools, web_tools, memory_tools, skill_tools
    file_tools.set_allowed_write_roots([Path(p) for p in cfg["allowed_write_roots"]])
    web_tools.set_searxng_url(cfg["searxng_url"])
    memory_tools.configure(cfg.get("project_memory_file", "GEMMA.md"), cfg.get("global_memory_file", ""))
    skill_tools.configure(cfg.get("allow_model_skills", True))
