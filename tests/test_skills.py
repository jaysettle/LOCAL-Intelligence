"""Tests for the skills system: discovery, the index, invocation and reporting.

No network and no Ollama. The global skills directory is redirected into tmp_path
so a test run never reads or writes the real config directory.
"""
import json

import pytest

from gemma_cli import skills
from gemma_cli.tools import skill_tools


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    global_dir = tmp_path / "globalskills"
    monkeypatch.setattr(skills, "global_skills_dir", lambda: global_dir)
    skill_tools.configure(True)
    return tmp_path


def _write(directory, name, text):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


SAMPLE = """---
name: weekly-report
description: Build the weekly status report
when: the user asks for a weekly summary
---
1. Run `git log --since='7 days ago'`
2. Summarise by area
"""


# --- parsing --------------------------------------------------------------

def test_parse_frontmatter(project):
    path = _write(project / "skills", "weekly-report", SAMPLE)
    skill = skills.parse_skill(path, "project")
    assert skill.name == "weekly-report"
    assert skill.description == "Build the weekly status report"
    assert skill.when == "the user asks for a weekly summary"
    assert "git log" in skill.body
    assert "---" not in skill.body  # frontmatter stripped


def test_parse_without_frontmatter_falls_back(project):
    path = _write(project / "skills", "adhoc", "# Deploy the thing\n\nRun the deploy script.\n")
    skill = skills.parse_skill(path, "project")
    assert skill.name == "adhoc"
    assert skill.description == "Deploy the thing"   # first meaningful line
    assert "Run the deploy script." in skill.body


def test_parse_rejects_empty_body(project):
    path = _write(project / "skills", "empty", "---\nname: empty\n---\n\n   \n")
    assert skills.parse_skill(path, "project") is None


def test_parse_ignores_unusable_frontmatter_name(project):
    """A hostile 'name' in frontmatter must not become the command name."""
    path = _write(project / "skills", "safe", "---\nname: ../../etc/passwd\n---\nbody here\n")
    skill = skills.parse_skill(path, "project")
    assert skill.name == "safe"  # falls back to the filename stem


def test_parse_survives_broken_yaml(project):
    path = _write(project / "skills", "broken", "---\nname: [unclosed\n---\nstill has a body\n")
    skill = skills.parse_skill(path, "project")
    assert skill is not None
    assert skill.name == "broken"


# --- discovery ------------------------------------------------------------

def test_discover_both_scopes(project):
    _write(project / "skills", "local-one", SAMPLE.replace("weekly-report", "local-one"))
    _write(skills.global_skills_dir(), "global-one", SAMPLE.replace("weekly-report", "global-one"))
    found = skills.discover()
    assert set(found) == {"local-one", "global-one"}
    assert found["local-one"].scope == "project"
    assert found["global-one"].scope == "global"


def test_project_skill_shadows_global(project):
    _write(skills.global_skills_dir(), "shared", "---\nname: shared\ndescription: global version\n---\nglobal body\n")
    _write(project / "skills", "shared", "---\nname: shared\ndescription: project version\n---\nproject body\n")
    found = skills.discover()
    assert len(found) == 1
    assert found["shared"].scope == "project"
    assert found["shared"].description == "project version"


def test_discover_empty_when_no_dir(project):
    assert skills.discover() == {}


# --- the system-prompt index ---------------------------------------------

def test_index_has_descriptions_but_never_bodies(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    block = skills.index_block()
    assert "weekly-report" in block
    assert "Build the weekly status report" in block
    assert "use when: the user asks for a weekly summary" in block
    assert "git log" not in block          # the body must NOT be inlined
    assert "Summarise by area" not in block


def test_index_empty_with_no_skills(project):
    assert skills.index_block() == ""


def test_index_caps_and_notes_overflow(project):
    for i in range(45):
        _write(project / "skills", f"skill-{i:02d}", f"---\nname: skill-{i:02d}\ndescription: d{i}\n---\nbody\n")
    block = skills.index_block(max_skills=40)
    assert "+5 more" in block


def test_index_stays_small(project):
    """The whole point: 20 skills must cost a few hundred characters, not thousands."""
    for i in range(20):
        body = "step\n" * 200
        _write(project / "skills", f"s{i:02d}", f"---\nname: s{i:02d}\ndescription: does thing {i}\n---\n{body}")
    block = skills.index_block()
    assert len(block) < 1500


def test_sysprompt_includes_the_index(project):
    from gemma_cli.config import load_config
    from gemma_cli.sysprompt import build_system_prompt

    _write(project / "skills", "weekly-report", SAMPLE)
    prompt = build_system_prompt(load_config())
    assert "weekly-report" in prompt
    assert "Build the weekly status report" in prompt
    assert "Summarise by area" not in prompt


# --- invocation -----------------------------------------------------------

def test_render_includes_body_and_extra(project):
    path = _write(project / "skills", "weekly-report", SAMPLE)
    skill = skills.parse_skill(path, "project")
    rendered = skill.render("focus on the parser work")
    assert "git log" in rendered
    assert "focus on the parser work" in rendered
    assert "# Skill: weekly-report" in rendered


def test_render_without_extra(project):
    path = _write(project / "skills", "weekly-report", SAMPLE)
    rendered = skills.parse_skill(path, "project").render()
    assert "THIS run" not in rendered


# --- saving ---------------------------------------------------------------

def test_save_round_trips(project):
    path = skills.save("deploy-api", "Deploy the API to staging", "1. Run tests\n2. Push", when="deploying")
    assert path == project / "skills" / "deploy-api.md"
    skill = skills.discover()["deploy-api"]
    assert skill.description == "Deploy the API to staging"
    assert skill.when == "deploying"
    assert "1. Run tests" in skill.body


def test_save_to_global_scope(project):
    skills.save("everywhere", "d", "body", scope="global")
    assert skills.discover()["everywhere"].scope == "global"


@pytest.mark.parametrize("bad", ["../evil", "a/b", "with space", "", "dot.name", "-leading"])
def test_save_rejects_unsafe_names(project, bad):
    with pytest.raises(ValueError):
        skills.save(bad, "d", "body")


def test_is_valid_name():
    assert skills.is_valid_name("weekly-report")
    assert skills.is_valid_name("deploy_api2")
    assert not skills.is_valid_name("../x")
    assert not skills.is_valid_name("has space")


# --- usage reporting ------------------------------------------------------

def test_record_and_report(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    skills.record_use("weekly-report", source="command")
    skills.record_use("weekly-report", source="model")
    rows = {r["name"]: r for r in skills.report()}
    assert rows["weekly-report"]["count"] == 2
    assert rows["weekly-report"]["sources"] == {"command": 1, "model": 1}
    assert rows["weekly-report"]["last_used"]


def test_report_sorted_by_use(project):
    for name in ("alpha", "beta"):
        _write(project / "skills", name, f"---\nname: {name}\ndescription: d\n---\nbody\n")
    skills.record_use("beta")
    assert [r["name"] for r in skills.report()] == ["beta", "alpha"]


def test_report_flags_deleted_skill_with_history(project):
    skills.record_use("ghost")
    rows = {r["name"]: r for r in skills.report()}
    assert rows["ghost"]["scope"] == "missing"


def test_usage_file_is_project_local(project):
    skills.record_use("x")
    assert (project / ".gemma" / "skill_usage.json").is_file()
    assert json.loads((project / ".gemma" / "skill_usage.json").read_text())["x"]["count"] == 1


def test_report_survives_corrupt_usage_file(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    usage = project / ".gemma" / "skill_usage.json"
    usage.parent.mkdir(parents=True, exist_ok=True)
    usage.write_text("{not json", encoding="utf-8")
    assert skills.report()[0]["count"] == 0


# --- the load_skill tool --------------------------------------------------

def test_load_skill_returns_body_and_records(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    out = skill_tools.load_skill({"name": "weekly-report"})
    assert "git log" in out
    assert "Build the weekly status report" in out
    assert skills.report()[0]["sources"] == {"model": 1}


def test_load_skill_unknown_lists_available(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    out = skill_tools.load_skill({"name": "nope"})
    assert "no skill named 'nope'" in out
    assert "weekly-report" in out


def test_load_skill_suggests_near_match(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    assert "Did you mean" in skill_tools.load_skill({"name": "weekly"})


def test_load_skill_when_none_exist(project):
    assert "no saved skills" in skill_tools.load_skill({"name": "anything"})


def test_load_skill_requires_name(project):
    assert "'name' is required" in skill_tools.load_skill({})


def test_load_skill_can_be_disabled(project):
    _write(project / "skills", "weekly-report", SAMPLE)
    skill_tools.configure(False)
    try:
        out = skill_tools.load_skill({"name": "weekly-report"})
        assert "disabled" in out
        assert "/weekly-report" in out
        assert "git log" not in out          # body withheld
        assert skills.report()[0]["count"] == 0   # and not counted
    finally:
        skill_tools.configure(True)


def test_config_switch_reaches_the_tool(project):
    from gemma_cli.config import load_config, apply_to_tools

    cfg = load_config({"allow_model_skills": False})
    apply_to_tools(cfg)
    try:
        assert skill_tools.ALLOW_MODEL_SKILLS is False
    finally:
        skill_tools.configure(True)


def test_tool_is_registered():
    from gemma_cli.tools import OLLAMA_TOOLS, execute_tool

    assert "load_skill" in [t["function"]["name"] for t in OLLAMA_TOOLS]
    assert "'name' is required" in execute_tool("load_skill", {})


# --- capture --------------------------------------------------------------

def test_parse_capture_extracts_all_three():
    parsed = skills.parse_capture(
        "DESCRIPTION: Ship a release\nWHEN: the user asks to cut a release\nBODY:\n1. Bump version\n2. Tag it"
    )
    assert parsed["description"] == "Ship a release"
    assert parsed["when"] == "the user asks to cut a release"
    assert parsed["body"] == "1. Bump version\n2. Tag it"


def test_parse_capture_strips_code_fences():
    parsed = skills.parse_capture("```markdown\nDESCRIPTION: X\nBODY:\n1. do it\n```")
    assert parsed["description"] == "X"
    assert parsed["body"] == "1. do it"


def test_parse_capture_without_labels_keeps_everything_as_body():
    parsed = skills.parse_capture("1. just steps\n2. no labels")
    assert parsed["body"].startswith("1. just steps")
    assert parsed["description"] == ""


def test_transcript_skips_system_and_notes_tool_calls():
    messages = [
        {"role": "system", "content": "SECRET SYSTEM PROMPT"},
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "shell", "arguments": {}}}]},
        {"role": "tool", "content": "output here"},
        {"role": "assistant", "content": "done"},
    ]
    text = skills.transcript_for_capture(messages)
    assert "SECRET SYSTEM PROMPT" not in text
    assert "[assistant called: shell]" in text
    assert "do the thing" in text
    assert "done" in text


def test_transcript_trims_to_the_recent_end():
    messages = [{"role": "user", "content": "x" * 500} for _ in range(100)]
    messages.append({"role": "assistant", "content": "FINAL ANSWER"})
    text = skills.transcript_for_capture(messages, max_chars=1000)
    assert len(text) < 1200
    assert "FINAL ANSWER" in text
    assert "earlier turns trimmed" in text


# --- REPL wiring ----------------------------------------------------------

def test_slash_command_runs_a_skill(project):
    from rich.console import Console
    from gemma_cli.main import _handle_command

    _write(project / "skills", "weekly-report", SAMPLE)
    console = Console(no_color=True)
    action, text, images = _handle_command(
        "/weekly-report focus on parsers", {"model": "m"}, [{"role": "system", "content": ""}],
        console, project / "s.json",
    )
    assert action == "run"
    assert "git log" in text
    assert "focus on parsers" in text
    assert skills.report()[0]["sources"] == {"command": 1}


def test_unknown_slash_command_is_not_a_skill(project):
    from rich.console import Console
    from gemma_cli.main import _handle_command

    action, text, _ = _handle_command(
        "/definitely-not-a-skill", {"model": "m"}, [{"role": "system", "content": ""}],
        Console(no_color=True), project / "s.json",
    )
    assert action == "handled"
    assert text is None
