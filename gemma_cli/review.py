#!/usr/bin/env python3
"""
`/check` — recursion as verification.

A second model call looks at the LAST turn only: the user's question, the tool
results the assistant gathered, and the answer it gave, and reports claims the
evidence does not support, steps that were skipped, numbers that do not match.
Small models hallucinate tool output and mark plan steps done that were not;
this catches the cheap, common cases.

It runs with thinking off (mechanical comparison work) and is user-invoked. It
never rewrites the answer — it prints a review the user can act on, so a wrong
review costs nothing.
"""

from typing import Any, Dict, List, Optional

_MAX_EVIDENCE = 6000
_MAX_ANSWER = 3000

_CHECK_PROMPT = """You are checking an AI assistant's last answer against the evidence it actually gathered.
Be strict and literal. Only the EVIDENCE counts — not what you believe is true.

QUESTION the user asked:
{question}

EVIDENCE (tool results the assistant received, possibly truncated):
{evidence}

ANSWER the assistant gave:
{answer}

Reply in exactly this format and nothing else:
VERDICT: <supported | partly supported | not supported>
ISSUES:
- <one line per problem: a claim the evidence does not support, a step the task needed that was not done, a number or name that differs from the evidence>
Write "- none" if there are no issues.
"""


def last_turn(messages: List[Dict]) -> Optional[Dict[str, Any]]:
    """Split the last turn into question / evidence / answer, or None if there isn't one."""
    last_user = None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user":
            content = str(m.get("content") or "")
            # Skip the harness's own nudges; they are not the user's question.
            if content.startswith("You returned nothing."):
                continue
            last_user = i
            break
    if last_user is None:
        return None

    question = str(messages[last_user].get("content") or "")
    evidence: List[str] = []
    answer = ""
    for m in messages[last_user + 1:]:
        role = m.get("role")
        if role == "tool":
            evidence.append(f"[{m.get('tool_name', 'tool')}] {str(m.get('content') or '').strip()}")
        elif role == "assistant":
            text = str(m.get("content") or "").strip()
            if text:
                answer = text
    return {"question": question, "evidence": evidence, "answer": answer}


def build_check_prompt(messages: List[Dict]) -> Optional[str]:
    turn = last_turn(messages)
    if turn is None or not turn["answer"]:
        return None
    evidence = "\n\n".join(turn["evidence"]) or "(the assistant used no tools)"
    if len(evidence) > _MAX_EVIDENCE:
        evidence = evidence[:_MAX_EVIDENCE] + "\n…(evidence truncated)"
    answer = turn["answer"]
    if len(answer) > _MAX_ANSWER:
        answer = answer[:_MAX_ANSWER] + " …(truncated)"
    return _CHECK_PROMPT.format(question=turn["question"].strip(), evidence=evidence, answer=answer)


def check_last_turn(cfg: Dict[str, Any], messages: List[Dict], console) -> Optional[str]:
    """Run the review and print it. Returns the raw review text, or None."""
    from .agent import _chat_once

    prompt = build_check_prompt(messages)
    if prompt is None:
        console.print("[dim]nothing to check yet — ask something first[/dim]")
        return None

    console.print("[dim]reviewing the last answer against its evidence…[/dim]")
    state = {"chars": 0, "dots": 0}

    def tick(piece: str) -> None:
        state["chars"] += len(piece)
        while state["chars"] // 60 > state["dots"]:
            state["dots"] += 1
            print(".", end="", flush=True)

    review_cfg = dict(cfg)
    review_cfg["thinking"] = False
    try:
        raw = _chat_once(review_cfg, [{"role": "user", "content": prompt}],
                         cfg.get("fast_model") or cfg["model"], on_token=tick)
    except Exception as e:
        if state["dots"]:
            print(flush=True)
        console.print(f"[red]could not run the check: {e}[/red]")
        return None
    if state["dots"]:
        print(flush=True)

    text = raw.strip() or "(the model returned nothing)"
    verdict = next((ln for ln in text.splitlines() if ln.upper().startswith("VERDICT")), "")
    colour = "green" if "not" not in verdict.lower() and "partly" not in verdict.lower() else "yellow"
    if "not supported" in verdict.lower():
        colour = "red"
    console.print(f"[{colour}]{verdict or 'review:'}[/{colour}]")
    for ln in text.splitlines():
        if ln.upper().startswith("VERDICT"):
            continue
        console.print(ln, markup=False, highlight=False)
    return text
