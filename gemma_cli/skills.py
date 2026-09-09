#!/usr/bin/env python3
"""
Skills: reusable procedures saved as markdown, invoked as /<skill-name>.

A skill is one .md file with YAML frontmatter and a body of instructions:

    ---
    name: weekly-report
    description: Build the weekly status report from git log and the tracker
    when: the user asks for a status report or a weekly summary
    ---
    1. Run `git log --since='7 days ago' --oneline`
    2. ...

Two scopes, project wins on a name collision:

    <cwd>/skills/<name>.md              project skills (commit these)
    <config_dir>/skills/<name>.md       global skills (all projects)

**Progressive disclosure is the whole design.** Only the index — name plus the
one-line description — goes into the system prompt, costing ~15 tokens per
skill. A skill's body is loaded only when it is actually invoked. Inlining
twenty skill bodies would eat half of a 32K context before the user has typed
anything; inlining twenty descriptions costs about 300 tokens.

Usage counts live in .gemma/skill_usage.json (project-local, gitignored) and
back the `/skills` report.
"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

SKILLS_DIRNAME = "skills"

# Skill names become slash commands and filenames: keep them boring and safe.
# Anchored, no dots or separators, so a name can never escape the skills dir.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    body: str
    path: Path
    scope: str = "project"          # "project" | "global"
    when: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def render(self, extra: str = "") -> str:
        """The message handed to the model when this skill is invoked."""
        parts = [
            f"Follow this saved skill exactly. It is a procedure you (or the user) "
            f"wrote earlier for this kind of task.\n",
            f"# Skill: {self.name}",
        ]
        if self.description:
            parts.append(f"_{self.description}_")
        parts.append("")
        parts.append(self.body.strip())
        if extra.strip():
            parts.append("")
            parts.append(f"The user added this for THIS run: {extra.strip()}")
        parts.append("")
        parts.append(
            "Work through the steps with your tools. Skip steps that clearly do not "
            "apply, and say so when you do."
        )
        return "\n".join(parts)


def is_valid_name(name: str) -> bool:
    return bool(_SAFE_NAME.match(name or ""))


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def project_skills_dir() -> Path:
    return Path.cwd() / SKILLS_DIRNAME


def global_skills_dir() -> Path:
    from .config import config_dir
    return config_dir() / SKILLS_DIRNAME


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def parse_skill(path: Path, scope: str) -> Optional[Skill]:
    """Parse one skill file. Returns None if it is unreadable or unusable."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    meta: Dict[str, Any] = {}
    body = raw
    match = _FRONTMATTER.match(raw)
    if match:
        try:
            loaded = yaml.safe_load(match.group(1)) or {}
            if isinstance(loaded, dict):
                meta = loaded
        except Exception:
            meta = {}
        body = match.group(2)

    name = str(meta.get("name") or path.stem).strip()
    if not is_valid_name(name):
        name = path.stem
    if not is_valid_name(name):
        return None

    description = str(meta.get("description") or "").strip()
    if not description:
        # Fall back to the first meaningful line so an unstructured file still
        # shows something useful in the index.
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                description = stripped[:120]
                break

    if not body.strip():
        return None

    return Skill(
        name=name,
        description=description,
        body=body.strip(),
        path=path,
        scope=scope,
        when=str(meta.get("when") or "").strip(),
        meta=meta,
    )


def discover() -> Dict[str, Skill]:
    """All available skills by name. A project skill shadows a global one."""
    found: Dict[str, Skill] = {}
    for scope, directory in (("global", global_skills_dir()), ("project", project_skills_dir())):
        try:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.md")):
                skill = parse_skill(path, scope)
                if skill:
                    found[skill.name] = skill
        except Exception:
            continue
    return found


def get(name: str) -> Optional[Skill]:
    return discover().get(name)


# ---------------------------------------------------------------------------
# The system-prompt index (names + descriptions only — never bodies)
# ---------------------------------------------------------------------------

def index_block(skills: Optional[Dict[str, Skill]] = None, max_skills: int = 40) -> str:
    """Compact skill index for the system prompt, or '' when there are none."""
    skills = discover() if skills is None else skills
    if not skills:
        return ""

    lines = []
    for skill in list(skills.values())[:max_skills]:
        entry = f"- {skill.name} — {skill.description}" if skill.description else f"- {skill.name}"
        if skill.when:
            entry += f" (use when: {skill.when})"
        lines.append(entry)
    if len(skills) > max_skills:
        lines.append(f"- ... (+{len(skills) - max_skills} more; run /skills to list them all)")

    return (
        "Saved skills — procedures already written for this project. The user runs one "
        "with /<name>. You can load one yourself with the load_skill tool, but ONLY when "
        "the request clearly matches its 'use when'; otherwise just do the task normally.\n"
        + "\n".join(lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Usage tracking (project-local, best-effort)
# ---------------------------------------------------------------------------

def _usage_path() -> Path:
    return Path.cwd() / ".gemma" / "skill_usage.json"


def _read_usage() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(_usage_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_use(name: str, source: str = "command") -> None:
    """Bump the counter for a skill. Never raises — reporting is not critical."""
    try:
        usage = _read_usage()
        entry = usage.get(name) or {"count": 0, "last_used": "", "sources": {}}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_used"] = datetime.now().isoformat(timespec="seconds")
        sources = entry.get("sources") or {}
        sources[source] = int(sources.get(source, 0)) + 1
        entry["sources"] = sources
        usage[name] = entry
        path = _usage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(usage, indent=1), encoding="utf-8")
    except Exception:
        pass


def report() -> List[Dict[str, Any]]:
    """Rows for the /skills report, most-used first then alphabetical."""
    skills = discover()
    usage = _read_usage()
    rows = []
    for name, skill in skills.items():
        stats = usage.get(name) or {}
        rows.append({
            "name": name,
            "scope": skill.scope,
            "description": skill.description,
            "when": skill.when,
            "path": str(skill.path),
            "count": int(stats.get("count", 0)),
            "last_used": stats.get("last_used", ""),
            "sources": stats.get("sources", {}),
        })
    rows.sort(key=lambda r: (-r["count"], r["name"]))

    # Surface counters for skills whose file has since been deleted, so the
    # report explains a number the user remembers seeing.
    for name, stats in usage.items():
        if name not in skills:
            rows.append({
                "name": name, "scope": "missing", "description": "(skill file no longer exists)",
                "when": "", "path": "", "count": int(stats.get("count", 0)),
                "last_used": stats.get("last_used", ""), "sources": stats.get("sources", {}),
            })
    return rows


# ---------------------------------------------------------------------------
# Writing skills
# ---------------------------------------------------------------------------

def save(name: str, description: str, body: str, when: str = "", scope: str = "project") -> Path:
    """Write a skill file, returning its path. Raises ValueError on a bad name."""
    if not is_valid_name(name):
        raise ValueError(
            f"'{name}' is not a usable skill name. Use letters, digits, hyphens or "
            "underscores (no spaces, dots or slashes)."
        )
    directory = global_skills_dir() if scope == "global" else project_skills_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"

    front = {"name": name, "description": description.strip()}
    if when.strip():
        front["when"] = when.strip()
    front["created"] = datetime.now().strftime("%Y-%m-%d")

    text = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + "---\n\n" + body.strip() + "\n"
    path.write_text(text, encoding="utf-8")
    return path


_CAPTURE_PROMPT = """Below is a transcript of work that was just completed.

Write a reusable SKILL from it: the procedure, generalised so it works next time
with different specifics. This is documentation for an AI agent that will follow
it later, with the same tools you have.

Rules:
- Numbered, imperative steps. Say which tool to use where it matters.
- Generalise. Replace this run's specific paths, names and values with a short
  note about what varies, but KEEP commands and file patterns that will be the
  same every time.
- Include anything that went wrong and how it was resolved — that is the most
  valuable part.
- No preamble, no sign-off. Start at step 1.
- Aim for 10-40 lines.

Reply with ONLY these three things, in this exact format:

DESCRIPTION: <one line, under 100 characters, what this skill does>
WHEN: <one line, when a request should trigger this skill>
BODY:
<the numbered steps>

Transcript:
{transcript}
"""


def transcript_for_capture(messages: List[Dict], max_chars: int = 12000) -> str:
    """Flatten a conversation into text for the capture prompt (newest kept)."""
    lines = []
    for message in messages:
        role = message.get("role", "")
        if role == "system":
            continue
        content = str(message.get("content") or "").strip()
        calls = message.get("tool_calls") or []
        if calls:
            names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
            lines.append(f"[assistant called: {names}]")
        if not content:
            continue
        if role == "tool":
            content = content[:400]
            lines.append(f"[tool result] {content}")
        else:
            lines.append(f"{role}: {content}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = "... (earlier turns trimmed)\n" + text[-max_chars:]
    return text


def parse_capture(raw: str) -> Dict[str, str]:
    """Pull DESCRIPTION / WHEN / BODY out of the model's capture reply."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)

    description, when = "", ""
    body = text

    match = re.search(r"^\s*DESCRIPTION:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
    if match:
        description = match.group(1).strip()
    match = re.search(r"^\s*WHEN:\s*(.+?)\s*$", text, re.MULTILINE | re.IGNORECASE)
    if match:
        when = match.group(1).strip()
    match = re.search(r"^\s*BODY:\s*\n(.*)$", text, re.DOTALL | re.MULTILINE | re.IGNORECASE)
    if match:
        body = match.group(1).strip()

    return {"description": description, "when": when, "body": body}


# ---------------------------------------------------------------------------
# Editor helper (also used by /memory)
# ---------------------------------------------------------------------------

def open_in_editor(path: Path) -> str:
    """Open a file for the human to edit. Returns a status line for the REPL."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
    except Exception as e:
        return f"could not create {path}: {e}"

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    try:
        if editor:
            subprocess.run([editor, str(path)])
            return f"edited {path}"
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
            return f"opened {path}"
        subprocess.run(["xdg-open", str(path)], capture_output=True)
        return f"opened {path}"
    except Exception as e:
        return f"could not open an editor ({e}). The file is at: {path}"
