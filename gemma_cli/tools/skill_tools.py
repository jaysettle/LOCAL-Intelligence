#!/usr/bin/env python3
"""
Skill tools: `load_skill` lets the model pull in a saved procedure on its own.

The system prompt carries only the skill INDEX (names + descriptions). When the
model decides a saved skill matches the request, it calls load_skill and gets the
full body back as a tool result — the progressive-disclosure half of skills.py.

Model-initiated loading can be switched off (`allow_model_skills: false` in
config.yaml) because a small model will happily over-trigger on a vague
description. With it off, skills still work via the /<name> command.
"""

from typing import Any, Dict

from .. import skills

# Set by config.apply_to_tools() at startup.
ALLOW_MODEL_SKILLS = True


def configure(allow_model_skills: bool) -> None:
    global ALLOW_MODEL_SKILLS
    ALLOW_MODEL_SKILLS = bool(allow_model_skills)


def load_skill(inp: Dict[str, Any]) -> str:
    name = str(inp.get("name", "")).strip()
    if not name:
        return "Error: 'name' is required (the skill to load)"

    if not ALLOW_MODEL_SKILLS:
        return (
            "Loading skills yourself is disabled in this configuration. Tell the user they "
            f"can run this skill themselves with: /{name}"
        )

    available = skills.discover()
    if not available:
        return "There are no saved skills in this project yet."

    skill = available.get(name)
    if skill is None:
        close = [n for n in available if name.lower() in n.lower() or n.lower() in name.lower()]
        suggestion = f" Did you mean: {', '.join(close)}?" if close else ""
        return (
            f"Error: no skill named '{name}'. Available: {', '.join(sorted(available))}.{suggestion}"
        )

    skills.record_use(skill.name, source="model")
    header = f"Skill '{skill.name}' ({skill.scope})"
    if skill.description:
        header += f" — {skill.description}"
    return (
        f"{header}\n\nFollow these steps, skipping any that clearly do not apply:\n\n"
        f"{skill.body}"
    )
