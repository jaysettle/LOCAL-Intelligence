#!/usr/bin/env python3
"""
Session persistence: save/restore a conversation so `gemma go --resume` can pick
up where you left off. Sessions are stored per-project under .gemma/sessions/.

A session file is JSON: {created, updated, cwd, model, messages}. The system
prompt (messages[0]) is NOT stored — it is rebuilt fresh each launch so the date,
working directory, and memory are always current. Only user/assistant/tool turns
are persisted.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


def session_dir() -> Path:
    return Path.cwd() / ".gemma" / "sessions"


def new_session_path() -> Path:
    return session_dir() / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"


def save_session(path: Path, messages: List[Dict], model: str) -> None:
    """Persist a conversation (excluding the system prompt at index 0)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = messages[1:] if messages and messages[0].get("role") == "system" else list(messages)
        created = _read_created(path)
        data = {
            "created": created,
            "updated": datetime.now().isoformat(timespec="seconds"),
            "cwd": str(Path.cwd()),
            "model": model,
            "messages": body,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass  # persistence is best-effort; never crash the session over it


def _read_created(path: Path) -> str:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("created", "")
        except Exception:
            pass
    return datetime.now().isoformat(timespec="seconds")


def list_sessions() -> List[Tuple[Path, Dict]]:
    """Return (path, metadata) for saved sessions in this project, newest first."""
    d = session_dir()
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        out.append((p, data))
    out.sort(key=lambda t: t[1].get("updated", ""), reverse=True)
    return out


def latest_session() -> Optional[Path]:
    sessions = list_sessions()
    return sessions[0][0] if sessions else None


def load_session(path: Path) -> List[Dict]:
    """Return the stored message turns (without the system prompt)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


def summarize(path: Path, meta: Dict) -> str:
    """A one-line description for the /sessions list."""
    msgs = meta.get("messages", [])
    first_user = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
    preview = (first_user[:50] + "…") if len(first_user) > 50 else first_user
    updated = meta.get("updated", "?")
    return f"{path.name}  [{updated}]  {len(msgs)} msgs  {preview!r}"
