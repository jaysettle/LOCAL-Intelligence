#!/usr/bin/env python3
"""
Lightweight in-session plan/todo scaffold. Small models stay on-task far better
with an explicit checklist in context. set_plan writes the list; complete_step
ticks items off. State lives for the process (one interactive session).
"""

from typing import Any, Dict, List

_PLAN: List[Dict[str, Any]] = []


def _render() -> str:
    if not _PLAN:
        return "(no plan set)"
    lines = [f"{'[x]' if s['done'] else '[ ]'} {i + 1}. {s['text']}" for i, s in enumerate(_PLAN)]
    remaining = sum(1 for s in _PLAN if not s["done"])
    return "\n".join(lines) + f"\n({remaining} of {len(_PLAN)} steps remaining)"


def set_plan(inp: Dict[str, Any]) -> str:
    steps = inp.get("steps") or []
    if isinstance(steps, str):
        steps = [steps]
    cleaned = [str(s).strip() for s in steps if str(s).strip()]
    if not cleaned:
        return "Error: 'steps' must be a non-empty list of step descriptions"
    _PLAN[:] = [{"text": s, "done": False} for s in cleaned]
    return "Plan set:\n" + _render()


def complete_step(inp: Dict[str, Any]) -> str:
    try:
        i = int(inp.get("index", 0))
    except (TypeError, ValueError):
        return "Error: 'index' must be an integer (1-based)"
    if not _PLAN:
        return "Error: no plan set. Call set_plan first."
    if not (1 <= i <= len(_PLAN)):
        return f"Error: step {i} out of range (1..{len(_PLAN)})"
    _PLAN[i - 1]["done"] = True
    return _render()


def reset_plan() -> None:
    _PLAN.clear()
