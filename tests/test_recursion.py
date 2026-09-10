"""Tests for recursive tooling: child runs, per-file skills, /check, and real
thinking control.

Everything model-facing is driven through a fake requests.post that records the
payloads sent to Ollama and replays scripted responses, so the tests pin the
wire-level facts (think:false only when disabling, narrowed tool lists, capped
budgets) without a model.
"""
import io
import json

import pytest
from rich.console import Console

from gemma_cli import agent, config, review, skills
from gemma_cli import main as main_mod
from gemma_cli.render import Renderer


# --- fake Ollama ------------------------------------------------------------

class _Resp:
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for c in self._chunks:
            yield json.dumps(c).encode()

    def close(self):
        pass


def _text_reply(text):
    return [{"message": {"content": text}}, {"done": True}]


def _tool_call(name, args):
    return [{"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}},
            {"done": True}]


def _fake_ollama(monkeypatch, script):
    """script: list of chunk-lists, replayed in order (last one repeats).

    Each recorded payload is a deep copy: run_turn keeps appending to the live
    message list after the post, so a reference would not show what was sent.
    """
    import copy
    sent = []

    def fake_post(url, json=None, stream=False, timeout=None):
        sent.append(copy.deepcopy(json))
        idx = min(len(sent) - 1, len(script) - 1)
        return _Resp(script[idx])

    monkeypatch.setattr(agent.requests, "post", fake_post)
    return sent


BASE = {"ollama_url": "http://x", "model": "m", "num_ctx": 1024, "allowed_write_roots": []}


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(skills, "global_skills_dir", lambda: tmp_path / "gskills")
    from gemma_cli.tools import file_tools, memory_tools
    file_tools.set_allowed_write_roots([tmp_path])
    memory_tools.configure("GEMMA.md", str(tmp_path / "mem.md"))
    return tmp_path


# --- thinking on the wire --------------------------------------------------

def test_think_omitted_by_default(monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    list(agent.run_turn(dict(BASE), [{"role": "system", "content": "s"}], "hi"))
    assert "think" not in sent[0]


def test_think_false_sent_when_disabled_in_config(monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    list(agent.run_turn(dict(BASE, thinking=False), [{"role": "system", "content": "s"}], "hi"))
    assert sent[0]["think"] is False


def test_think_override_beats_config(monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    list(agent.run_turn(dict(BASE, thinking=True), [{"role": "system", "content": "s"}], "hi", think=False))
    assert sent[0]["think"] is False


def test_chat_once_honours_thinking(monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    agent._chat_once(dict(BASE, thinking=False), [{"role": "user", "content": "x"}], "m")
    agent._chat_once(dict(BASE), [{"role": "user", "content": "x"}], "m")
    assert sent[0]["think"] is False and "think" not in sent[1]


def test_config_thinking_default_and_env(project, monkeypatch):
    assert config.load_config()["thinking"] is True
    monkeypatch.setenv("GEMMA_THINKING", "false")
    assert config.load_config()["thinking"] is False
    monkeypatch.setenv("GEMMA_THINKING", "0")
    assert config.load_config()["thinking"] is False
    monkeypatch.setenv("GEMMA_THINKING", "yes")
    assert config.load_config()["thinking"] is True


def test_no_thinking_flag_disables_generation(project, monkeypatch):
    seen = {}
    monkeypatch.setattr(main_mod, "_run_once",
                        lambda cfg, *a, **k: seen.update(thinking=cfg.get("thinking"), show=cfg.get("show_thinking")))
    assert main_mod.main(["-p", "hello", "--no-thinking"]) == 0
    assert seen == {"thinking": False, "show": False}


# --- run_turn overrides -----------------------------------------------------

def test_run_turn_uses_the_given_tool_list(monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    tools = [t for t in agent.OLLAMA_TOOLS if t["function"]["name"] == "read_file"]
    list(agent.run_turn(dict(BASE), [{"role": "system", "content": "s"}], "hi", tools=tools))
    assert [t["function"]["name"] for t in sent[0]["tools"]] == ["read_file"]


def test_run_turn_max_iters_override(monkeypatch):
    # The model keeps calling a tool forever; the override must stop it at 2 posts.
    sent = _fake_ollama(monkeypatch, [_tool_call("list_directory", {"path": "."})])
    events = list(agent.run_turn(dict(BASE), [{"role": "system", "content": "s"}], "hi", max_iters=2))
    assert len(sent) == 2
    assert any(k == "text" and "tool-call limit" in str(p) for k, p in events)


# --- run_child --------------------------------------------------------------

def test_child_runs_in_a_fresh_context_with_thinking_off(project, monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("Sensor units $4,200")])
    parent = [{"role": "system", "content": "PARENT"}, {"role": "user", "content": "earlier"}]
    holder = {}
    events = list(agent.run_child(dict(BASE), "summarise costs.xlsx", label="[1/1] costs.xlsx", out=holder))

    payload = sent[0]
    assert payload["think"] is False                       # child_thinking defaults off
    assert payload["messages"][0]["role"] == "system"
    assert "PARENT" not in payload["messages"][0]["content"]   # fresh system prompt, not the parent's
    assert all(m.get("content") != "earlier" for m in payload["messages"])  # no parent history
    assert parent == [{"role": "system", "content": "PARENT"}, {"role": "user", "content": "earlier"}]

    kinds = [k for k, _ in events]
    assert kinds[0] == "child" and kinds[-1] == "child_result"
    assert holder["result"] == "Sensor units $4,200"
    assert events[-1][1] == {"label": "[1/1] costs.xlsx", "result": "Sensor units $4,200"}


def test_child_thinking_can_be_turned_on(project, monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    list(agent.run_child(dict(BASE, child_thinking=True), "x"))
    assert "think" not in sent[0]


def test_child_result_is_capped(project, monkeypatch):
    _fake_ollama(monkeypatch, [_text_reply("y" * 500)])
    holder = {}
    list(agent.run_child(dict(BASE, child_result_chars=100), "x", out=holder))
    assert len(holder["result"]) < 130 and holder["result"].endswith("…(truncated)")


def test_child_iteration_budget_is_smaller(project, monkeypatch):
    sent = _fake_ollama(monkeypatch, [_tool_call("list_directory", {"path": "."})])
    list(agent.run_child(dict(BASE, child_max_tool_iterations=3), "x"))
    assert len(sent) == 3


def test_child_excludes_forbidden_tools(project, monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    monkeypatch.setattr(agent, "CHILD_EXCLUDED_TOOLS", frozenset({"shell", "delete_file"}))
    list(agent.run_child(dict(BASE), "x"))
    names = {t["function"]["name"] for t in sent[0]["tools"]}
    assert "shell" not in names and "delete_file" not in names and "read_document" in names


def test_child_context_is_prepended(project, monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    list(agent.run_child(dict(BASE), "do the thing", context="The user wants a summary."))
    user = [m for m in sent[0]["messages"] if m["role"] == "user"][-1]
    assert user["content"].startswith("The user wants a summary.")
    assert user["content"].endswith("do the thing")


# --- per-file skills ---------------------------------------------------------

PER_FILE = """---
name: doc-lines
description: One line per document
mode: per-file
glob: "*.pdf, *.docx"
---
1. Read the file with read_document.
2. Reply with one line: what it is and the key number in it.
"""


def _write_skill(project, text, name="doc-lines"):
    (project / "skills").mkdir(exist_ok=True)
    (project / "skills" / f"{name}.md").write_text(text, encoding="utf-8")


def test_per_file_frontmatter_parses(project):
    _write_skill(project, PER_FILE)
    s = skills.discover()["doc-lines"]
    assert s.per_file and s.mode == "per-file"
    assert s.globs == ["*.pdf", "*.docx"]


def test_glob_accepts_a_yaml_list(project):
    _write_skill(project, PER_FILE.replace('glob: "*.pdf, *.docx"', "globs:\n  - '*.pdf'\n  - '*.xlsx'"))
    assert skills.discover()["doc-lines"].globs == ["*.pdf", "*.xlsx"]


def test_discover_items_files_only_sorted_capped(project):
    _write_skill(project, PER_FILE)
    for n in ("b.pdf", "a.docx", "c.pdf", "skip.txt"):
        (project / n).write_text("x")
    (project / "d.pdf").mkdir()          # a directory named like a match
    s = skills.discover()["doc-lines"]
    items, total = skills.discover_items(s, limit=2)
    assert total == 3
    assert [p.name for p in items] == ["a.docx", "b.pdf"]


def test_render_item_and_synthesis(project):
    _write_skill(project, PER_FILE)
    s = skills.discover()["doc-lines"]
    item = s.render_item(project / "a.pdf", extra="be terse")
    assert "ONE item" in item and "a.pdf" in item and "read_document" in item and "be terse" in item
    synth = s.render_synthesis([("a.pdf", "invoice, $4,510"), ("b.docx", "report\nmultiline")])
    assert "- a.pdf: invoice, $4,510" in synth
    assert "- b.docx: report multiline" in synth
    assert "Do NOT re-read" in synth


def test_slash_command_expands_a_per_file_skill(project):
    _write_skill(project, PER_FILE)
    for n in ("b.pdf", "a.docx"):
        (project / n).write_text("x")
    action, job, images = main_mod._handle_command(
        "/doc-lines keep it short", {"model": "m", "per_file_max_items": 12},
        [{"role": "system", "content": ""}], Console(no_color=True, file=io.StringIO()), None)
    assert action == "per_item"
    assert [p.name for p in job["items"]] == ["a.docx", "b.pdf"]
    assert job["skill"].name == "doc-lines" and job["extra"] == "keep it short"


def test_slash_command_with_no_matches_is_handled(project):
    _write_skill(project, PER_FILE)
    out = io.StringIO()
    action, *_ = main_mod._handle_command("/doc-lines", {"model": "m"}, [{"role": "system", "content": ""}],
                                          Console(no_color=True, file=out), None)
    assert action == "handled" and "no files match" in out.getvalue()


def test_ordinary_skill_still_runs_as_one_turn(project):
    plain = PER_FILE.replace("mode: per-file\n", "").replace("name: doc-lines", "name: plain")
    _write_skill(project, plain, name="plain")
    action, text, _ = main_mod._handle_command("/plain", {"model": "m"}, [{"role": "system", "content": ""}],
                                               Console(no_color=True, file=io.StringIO()), None)
    assert action == "run" and "# Skill: plain" in text


def test_per_item_events_children_then_synthesis(project, monkeypatch):
    """N children (fresh contexts) then ONE parent turn recorded in the session."""
    _write_skill(project, PER_FILE)
    for n in ("a.pdf", "b.pdf"):
        (project / n).write_text("x")
    s = skills.discover()["doc-lines"]
    items, _ = skills.discover_items(s)

    posts = _fake_ollama(monkeypatch, [_text_reply("child says A"), _text_reply("child says B"),
                                       _text_reply("FINAL: A and B")])
    messages = [{"role": "system", "content": "PARENT"}]
    events = list(main_mod._per_item_events(dict(BASE), messages, {"skill": s, "items": items, "extra": ""}))

    kinds = [k for k, _ in events]
    assert kinds[0] == "notice" and kinds.count("child_result") == 2
    results = [p["result"] for k, p in events if k == "child_result"]
    assert results == ["child says A", "child says B"]

    # Children never touched the parent transcript; the synthesis turn did.
    assert len(posts) == 3
    assert posts[0]["think"] is False and posts[1]["think"] is False   # children: thinking off
    assert "think" not in posts[2]                                       # parent synthesis: default
    assert messages[1]["role"] == "user" and "- a.pdf: child says A" in messages[1]["content"]
    assert messages[-1] == {"role": "assistant", "content": "FINAL: A and B"}


# --- read-only children ------------------------------------------------------

def test_readonly_child_has_no_mutating_tools(project, monkeypatch):
    sent = _fake_ollama(monkeypatch, [_text_reply("ok")])
    list(agent.run_child(dict(BASE), "x", readonly=True))
    names = {t["function"]["name"] for t in sent[0]["tools"]}
    assert not (names & {"write_file", "edit_file", "delete_file", "shell"})
    assert {"read_document", "read_file", "list_directory", "glob", "grep"} <= names


def test_per_file_children_are_readonly_by_default(project, monkeypatch):
    """Observed live: the first child wrote INDEX.md with its one line."""
    _write_skill(project, PER_FILE)
    (project / "a.pdf").write_text("x")
    s = skills.discover()["doc-lines"]
    items, _ = skills.discover_items(s)
    posts = _fake_ollama(monkeypatch, [_text_reply("child"), _text_reply("FINAL")])
    events = list(main_mod._per_item_events(dict(BASE), [{"role": "system", "content": "P"}],
                                            {"skill": s, "items": items, "extra": ""}))
    child_tools = {t["function"]["name"] for t in posts[0]["tools"]}
    parent_tools = {t["function"]["name"] for t in posts[1]["tools"]}
    assert "write_file" not in child_tools and "shell" not in child_tools
    assert "write_file" in parent_tools            # the synthesis turn may write
    assert "read-only" in events[0][1]


def test_skill_can_opt_children_into_writing(project, monkeypatch):
    _write_skill(project, PER_FILE.replace("mode: per-file\n", "mode: per-file\nchild_writes: true\n"))
    (project / "a.pdf").write_text("x")
    s = skills.discover()["doc-lines"]
    assert s.child_writes is True
    items, _ = skills.discover_items(s)
    posts = _fake_ollama(monkeypatch, [_text_reply("child"), _text_reply("FINAL")])
    list(main_mod._per_item_events(dict(BASE), [{"role": "system", "content": "P"}],
                                   {"skill": s, "items": items, "extra": ""}))
    assert "write_file" in {t["function"]["name"] for t in posts[0]["tools"]}


def test_config_can_disable_readonly(project, monkeypatch):
    _write_skill(project, PER_FILE)
    (project / "a.pdf").write_text("x")
    s = skills.discover()["doc-lines"]
    items, _ = skills.discover_items(s)
    posts = _fake_ollama(monkeypatch, [_text_reply("child"), _text_reply("FINAL")])
    list(main_mod._per_item_events(dict(BASE, child_readonly=False), [{"role": "system", "content": "P"}],
                                   {"skill": s, "items": items, "extra": ""}))
    assert "write_file" in {t["function"]["name"] for t in posts[0]["tools"]}


def test_item_prompt_tells_the_child_to_skip_aggregate_steps(project):
    _write_skill(project, PER_FILE)
    s = skills.discover()["doc-lines"]
    assert "NOT yours" in s.render_item(project / "a.pdf")


# --- rendering child events -------------------------------------------------

def test_renderer_shows_child_tools_and_results_compactly():
    console = Console(file=io.StringIO(), width=80, no_color=True, color_system=None, highlight=False)
    Renderer(console).consume(iter([
        ("notice", "doc-lines: 1 file(s), each in its own context, thinking off"),
        ("child", ("tool_start", {"name": "read_document", "args": {"path": "a.pdf"}})),
        ("child", ("tool_result", {"result": "a.pdf [pdf, 1 pages]"})),
        ("child", ("text", "this child text is hidden unless --verbose")),
        ("child_result", {"label": "[1/1] a.pdf", "result": "invoice, $4,510\nmore"}),
        ("text", "FINAL"), ("done", None),
    ]))
    out = console.file.getvalue()
    assert "↳ read_document a.pdf" in out
    assert "[1/1] a.pdf: invoice, $4,510" in out
    assert "hidden unless" not in out
    assert "FINAL" in out


def test_consume_plain_shows_child_events(capsys):
    main_mod._consume_plain(iter([
        ("child", ("tool_start", {"name": "read_document", "args": {"path": "a.pdf"}})),
        ("child_result", {"label": "[1/2] a.pdf", "result": "one line"}),
        ("text", "FINAL\n"), ("done", None),
    ]))
    out = capsys.readouterr().out
    assert "-> read_document a.pdf" in out and "[1/2] a.pdf: one line" in out and "FINAL" in out


# --- /check -----------------------------------------------------------------

MESSAGES = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "old question"},
    {"role": "assistant", "content": "old answer"},
    {"role": "user", "content": "what is the total?"},
    {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_document", "arguments": {}}}]},
    {"role": "tool", "tool_name": "read_document", "content": "Sensor units | 4200\nShipping | 310"},
    {"role": "assistant", "content": "The total is $4,510."},
]


def test_build_check_prompt_uses_only_the_last_turn():
    prompt = review.build_check_prompt(MESSAGES)
    assert "what is the total?" in prompt
    assert "[read_document] Sensor units | 4200" in prompt
    assert "The total is $4,510." in prompt
    assert "old question" not in prompt and "old answer" not in prompt


def test_build_check_prompt_none_when_nothing_to_check():
    assert review.build_check_prompt([{"role": "system", "content": "s"}]) is None
    assert review.build_check_prompt([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]) is None


def test_check_last_turn_runs_without_thinking_and_prints_verdict(monkeypatch):
    seen = {}

    def fake_chat_once(cfg, messages, model, on_token=None):
        seen["thinking"] = cfg.get("thinking")
        return "VERDICT: supported\nISSUES:\n- none"

    monkeypatch.setattr(agent, "_chat_once", fake_chat_once)
    out = io.StringIO()
    text = review.check_last_turn(dict(BASE, thinking=True), MESSAGES, Console(no_color=True, file=out))
    assert seen["thinking"] is False
    assert "VERDICT: supported" in out.getvalue() and "- none" in out.getvalue()
    assert text.startswith("VERDICT")


def test_check_command_is_wired(monkeypatch):
    monkeypatch.setattr(review, "check_last_turn", lambda cfg, messages, console: "VERDICT: supported")
    action, *_ = main_mod._handle_command("/check", {"model": "m"}, list(MESSAGES),
                                          Console(no_color=True, file=io.StringIO()), None)
    assert action == "handled"
