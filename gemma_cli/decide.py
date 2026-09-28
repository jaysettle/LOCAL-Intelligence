#!/usr/bin/env python3
"""
Fast typed decisions - "System One" questions - for gemma's own control flow.

A decision is a narrow question with a fixed set of answers: pick one option,
rate on a scale, or yes/no. It comes back as probabilities, not prose, so code
can act on it. The request and answer shapes are TypeSafe's System One wire
format (the API of their Jev model), so one caller works with every backend:

* "gemma" (default): the model gemma already runs, through Ollama. The options
  become letters, Ollama generates three tokens with thinking off, and the
  probability of each letter is read from the token scores (logprobs) at the
  first position that holds a letter - gemma4 writes "(C)", so position 0 is a
  bracket. A community JevBench entry does the same with an unmodified
  Gemma-4-12B. Measured on an RTX 2070 laptop: ~0.5 s per question once the
  model is loaded. Private and free; nothing leaves the machine. Its
  probabilities are overconfident (99%+ nearly always), so thresholds need
  checking against real prompts - which is what shadow mode is for.
* "systemone": any server speaking POST /v1/systemone - TypeSafe's Jev API, a
  gateway serving Jev, or a local open decider model. OFF unless configured.
  With TypeSafe's cloud the question text LEAVES THIS MACHINE; gemma says so the
  first time it happens in a session.

A decision never breaks a turn: any failure returns an answer marked unknown.
The model never decides when to ask - code asks at fixed points, like per-file
skills, because a 12B over-uses optional machinery.
"""

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
MAX_LOCAL_OPTIONS = len(LETTERS)
LOG_NAME = "decisions.jsonl"


@dataclass
class Answer:
    type: str                                   # "choice" | "score" | "noul"
    value: Any = None                           # choice: option key; score: float; noul: P(yes)
    probabilities: Dict[str, float] = field(default_factory=dict)
    confidence: Optional[float] = None          # 0-1; None when unknown
    error: str = ""

    @property
    def known(self) -> bool:
        return not self.error and self.value is not None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": self.type, self.type: self.value,
                               "probabilities": self.probabilities, "confidence": self.confidence}
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Decision:
    answers: Dict[str, Answer]
    backend: str
    model: str
    seconds: float


# ---------------------------------------------------------------------------
# Question shapes (TypeSafe wire format)
# ---------------------------------------------------------------------------

def choice(instructions: str, criteria: Dict[str, str]) -> Dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, levels: List[str]) -> Dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions: str, true: str = "", false: str = "") -> Dict[str, Any]:
    q: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true or "yes", "false": false or "no"}
    return q


def _options(q: Dict[str, Any]) -> List[tuple]:
    """(key, description) pairs for a question, in order."""
    kind = q.get("type")
    crit = q.get("criteria")
    if kind == "choice":
        return list((crit or {}).items())
    if kind == "score":
        return [(str(i), str(d)) for i, d in enumerate(crit or [])]
    if kind == "noul":
        c = crit or {}
        return [("true", str(c.get("true") or "Yes")), ("false", str(c.get("false") or "No"))]
    raise ValueError(f"unknown question type {kind!r}")


def _spread_confidence(probs: List[float]) -> float:
    """How concentrated a distribution is: 1 = all on one option, 0 = even.
    (TypeSafe's documented statistic, generalised to k options.)"""
    k = len(probs)
    if k < 2:
        return 1.0
    return max(0.0, min(1.0, (k * max(probs) - 1) / (k - 1)))


# ---------------------------------------------------------------------------
# Backend: gemma's own model through Ollama
# ---------------------------------------------------------------------------

def _state_text(state: Any) -> str:
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, indent=1)


def _ask_ollama(cfg: Dict[str, Any], state: Any, q: Dict[str, Any]) -> Answer:
    opts = _options(q)
    kind = q["type"]
    if not 2 <= len(opts) <= MAX_LOCAL_OPTIONS:
        return Answer(kind, error=f"needs 2-{MAX_LOCAL_OPTIONS} options, got {len(opts)}")
    letters = LETTERS[:len(opts)]
    lines = "\n".join(f"({l}) {desc}" for l, (_, desc) in zip(letters, opts))
    prompt = (f"Context:\n{_state_text(state)}\n\nQuestion: {q.get('instructions', '')}\n"
              f"Options:\n{lines}\n\nReply with only the letter of the best option.")
    payload = {
        "model": cfg.get("decide_model") or cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "keep_alive": cfg.get("keep_alive", "30m"),
        "logprobs": True,
        "top_logprobs": 20,
        # num_ctx must match the chat's, or Ollama reloads the model for this call
        "options": {"num_predict": 3, "temperature": 0, "num_ctx": int(cfg.get("num_ctx", 32768))},
    }
    url = f"{cfg['ollama_url'].rstrip('/')}/api/chat"
    resp = requests.post(url, json=payload, timeout=float(cfg.get("decide_timeout", 60)))
    resp.raise_for_status()
    positions = resp.json().get("logprobs") or []
    if not positions:
        return Answer(kind, error="Ollama returned no token probabilities (needs Ollama 0.12.11+)")
    for pos in positions:
        token = pos.get("token", "").strip().strip("(*").upper()
        if len(token) != 1 or token not in letters:       # "" is `in` every string: check length
            continue
        mass: Dict[str, float] = {}
        for alt in pos.get("top_logprobs") or []:
            letter = alt.get("token", "").strip().strip("(*").upper()
            if len(letter) == 1 and letter in letters:
                mass[letter] = mass.get(letter, 0.0) + math.exp(alt["logprob"])
        total = sum(mass.values())
        if total <= 0:
            break
        probs = {key: mass.get(l, 0.0) / total for l, (key, _) in zip(letters, opts)}
        return _answer(kind, probs)
    return Answer(kind, error="the model did not answer with an option letter")


def _answer(kind: str, probs: Dict[str, float]) -> Answer:
    vals = list(probs.values())
    if kind == "noul":
        p = probs.get("true", 0.0)
        return Answer(kind, round(p, 4), {k: round(v, 4) for k, v in probs.items()}, round(abs(2 * p - 1), 4))
    if kind == "score":
        expected = sum(int(k) * v for k, v in probs.items())
        return Answer(kind, round(expected, 3), {k: round(v, 4) for k, v in probs.items()},
                      round(_spread_confidence(vals), 4))
    best = max(probs, key=probs.get)
    return Answer(kind, best, {k: round(v, 4) for k, v in probs.items()}, round(_spread_confidence(vals), 4))


# ---------------------------------------------------------------------------
# Backend: a System One server (TypeSafe Jev, or anything speaking its format)
# ---------------------------------------------------------------------------

def api_key(cfg: Dict[str, Any]) -> str:
    return os.environ.get(str(cfg.get("decide_api_key_env") or "TYPESAFE_API_KEY"), "")


def is_remote(cfg: Dict[str, Any]) -> bool:
    """True when decisions would be sent to another machine."""
    if cfg.get("decide_backend") != "systemone":
        return False
    host = (urlparse(str(cfg.get("decide_url") or "")).hostname or "").lower()
    return host not in ("localhost", "127.0.0.1", "::1", "")


def _ask_systemone(cfg: Dict[str, Any], state: Any, questions: Dict[str, Dict]) -> Dict[str, Answer]:
    base = str(cfg.get("decide_url") or "https://api.typesafe.ai").rstrip("/")
    url = base if base.endswith("/v1/systemone") else base + "/v1/systemone"
    headers = {"Content-Type": "application/json"}
    key = api_key(cfg)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": cfg.get("decide_model") or "jev-latest", "state": state, "questions": questions}
    resp = requests.post(url, json=body, headers=headers, timeout=float(cfg.get("decide_timeout", 60)))
    if resp.status_code == 401:
        raise RuntimeError(f"401 from {base}: missing or invalid API key "
                           f"(set {cfg.get('decide_api_key_env') or 'TYPESAFE_API_KEY'})")
    resp.raise_for_status()
    raw = resp.json().get("answers") or {}
    out: Dict[str, Answer] = {}
    for qid, q in questions.items():
        a = raw.get(qid)
        if not isinstance(a, dict):
            out[qid] = Answer(q["type"], error="no answer returned")
            continue
        kind = a.get("type", q["type"])
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()}
        value = a.get(kind)
        conf = a.get("confidence")
        if kind == "noul" and value is not None:
            probs = probs or {"true": float(value), "false": 1 - float(value)}
            conf = abs(2 * float(value) - 1) if conf is None else conf
        out[qid] = Answer(kind, value, probs, conf)
    return out


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------

def decide(cfg: Dict[str, Any], state: Any, questions: Dict[str, Dict]) -> Decision:
    """Ask typed questions about `state`. Never raises: failures come back as
    answers with .error set (and .known False)."""
    backend = str(cfg.get("decide_backend") or "gemma")
    start = time.perf_counter()
    answers: Dict[str, Answer] = {}
    model = cfg.get("decide_model") or (cfg.get("model") if backend == "gemma" else "jev-latest")
    try:
        if backend == "systemone":
            answers = _ask_systemone(cfg, state, questions)
        else:
            for qid, q in questions.items():
                try:
                    answers[qid] = _ask_ollama(cfg, state, q)
                except Exception as e:                       # one bad question must not sink the rest
                    answers[qid] = Answer(q.get("type", "?"), error=_short(e))
    except Exception as e:
        answers = {qid: Answer(q.get("type", "?"), error=_short(e)) for qid, q in questions.items()}
    return Decision(answers, backend, str(model), round(time.perf_counter() - start, 3))


def _short(e: Exception) -> str:
    text = str(e) or type(e).__name__
    return text if len(text) <= 200 else text[:200] + "..."


# ---------------------------------------------------------------------------
# Decision points
# ---------------------------------------------------------------------------

THINKING_QUESTION = choice(
    "How much reasoning does the assistant need before answering this request?",
    {
        "simple": "Little: a greeting, a fact, a definition, a short lookup, or one direct action "
                  "such as reading, listing or opening a file.",
        "reasoning": "Careful step-by-step work: comparing, analysing, planning, debugging, "
                     "calculating, writing something substantial, or working across several files.",
    },
)


def thinking_check(cfg: Dict[str, Any], user_text: str) -> Decision:
    """Would this prompt be fine without thinking? (the first decision point)."""
    state = {"request": (user_text or "")[:4000]}
    return decide(cfg, state, {"effort": THINKING_QUESTION})


def skip_thinking(cfg: Dict[str, Any], d: Decision) -> bool:
    """True only for a confident 'simple'. Unknown or unsure keeps thinking on."""
    a = d.answers.get("effort")
    if a is None or not a.known or a.value != "simple":
        return False
    return (a.confidence or 0.0) >= float(cfg.get("decide_threshold", 0.8))


# ---------------------------------------------------------------------------
# The shadow log: what each decision said, and how the turn went
# ---------------------------------------------------------------------------

def log_path(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / ".gemma" / LOG_NAME


def log_decision(record: Dict[str, Any], cwd: Optional[str] = None) -> None:
    """Append one JSON line. Never raises: the log must not break a turn."""
    try:
        p = log_path(cwd)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_log(limit: int = 20, cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    p = log_path(cwd)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]
