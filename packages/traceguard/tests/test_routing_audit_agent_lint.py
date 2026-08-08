"""agent_lint: which agent definitions leave model: unset.

The distinction under test throughout is three-state, not two: absent,
explicitly inherited, and pinned. Collapsing the first two is the mistake
that would make the lint useless, because "nobody filled this in" and
"someone decided to inherit" need different answers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from traceguard.routing_audit.agent_lint import (
    AgentDef,
    format_report,
    main,
    parse_frontmatter,
    read_agent,
    scan,
    summarize,
)


def write_agent(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------- frontmatter


def test_parse_frontmatter_reads_top_level_scalars():
    fields = parse_frontmatter("---\nname: alpha\nmodel: sonnet\n---\nbody\n")
    assert fields == {"name": "alpha", "model": "sonnet"}


def test_parse_frontmatter_requires_the_block_to_start_line_one():
    assert parse_frontmatter("\n---\nname: alpha\n---\n") is None
    assert parse_frontmatter("# heading\nname: alpha\n") is None


def test_parse_frontmatter_returns_none_when_the_block_never_closes():
    assert parse_frontmatter("---\nname: alpha\nmodel: sonnet\n") is None


def test_parse_frontmatter_keeps_colons_inside_a_value():
    fields = parse_frontmatter("---\ndescription: Use when: you must.\nmodel: opus\n---\n")
    assert fields["description"] == "Use when: you must."
    assert fields["model"] == "opus"


def test_parse_frontmatter_strips_quotes_and_skips_nested_and_list_lines():
    fields = parse_frontmatter(
        "---\n"
        'name: "alpha"\n'
        "tools:\n"
        "  - Read\n"
        "  - Grep\n"
        "model: 'haiku'\n"
        "# a comment\n"
        "---\n"
    )
    assert fields["name"] == "alpha"
    assert fields["model"] == "haiku"
    assert "- Read" not in fields


# ---------------------------------------------------------------- three states


@pytest.mark.parametrize(
    "frontmatter,expected",
    [
        ("name: a\nmodel: sonnet", "pinned"),
        ("name: a", "unpinned"),
        ("name: a\nmodel:", "unpinned"),
        ("name: a\nmodel: inherit", "inherit"),
        ("name: a\nmodel: INHERIT", "inherit"),
        ("name: a\nmodel: parent", "inherit"),
    ],
)
def test_state_classification(tmp_path, frontmatter, expected):
    path = write_agent(tmp_path, "a.md", f"---\n{frontmatter}\n---\nbody\n")
    assert read_agent(path).state == expected


def test_empty_model_value_is_unpinned_not_pinned(tmp_path):
    """An empty value behaves exactly like an absent key at runtime, so the
    lint must not let ``model:`` with nothing after it pass as a decision."""
    path = write_agent(tmp_path, "a.md", "---\nname: a\nmodel:\n---\n")
    agent = read_agent(path)
    assert agent.model is None
    assert agent.state == "unpinned"


def test_explicit_inherit_is_reported_apart_from_unpinned(tmp_path):
    write_agent(tmp_path, "chosen.md", "---\nname: chosen\nmodel: inherit\n---\n")
    write_agent(tmp_path, "forgotten.md", "---\nname: forgotten\n---\n")
    counts = summarize(scan([tmp_path]))
    assert counts["inherit"] == 1
    assert counts["unpinned"] == 1


def test_file_without_frontmatter_is_malformed_not_unpinned(tmp_path):
    write_agent(tmp_path, "readme.md", "# Just notes about the agents\n")
    (agent,) = scan([tmp_path])
    assert agent.state == "malformed"
    assert summarize([agent])["unpinned"] == 0


def test_name_falls_back_to_the_filename(tmp_path):
    path = write_agent(tmp_path, "no-name-key.md", "---\nmodel: opus\n---\n")
    assert read_agent(path).name == "no-name-key"


# ---------------------------------------------------------------- scanning


def test_scan_recurses_into_subdirectories(tmp_path):
    write_agent(tmp_path, "top.md", "---\nname: top\nmodel: opus\n---\n")
    write_agent(tmp_path / "team", "nested.md", "---\nname: nested\n---\n")
    names = {d.name for d in scan([tmp_path])}
    assert names == {"top", "nested"}


def test_scan_skips_missing_roots_without_raising(tmp_path):
    write_agent(tmp_path, "a.md", "---\nname: a\nmodel: opus\n---\n")
    assert len(scan([tmp_path / "nope", tmp_path])) == 1


def test_scan_reports_each_file_once_across_overlapping_roots(tmp_path):
    """Passing a project dir and a parent that contains it is a normal thing
    to do; the same definition must not be counted twice."""
    inner = tmp_path / "nested"
    write_agent(inner, "a.md", "---\nname: a\n---\n")
    assert len(scan([tmp_path, inner])) == 1


def test_scan_ignores_non_markdown(tmp_path):
    (tmp_path / "a.json").write_text('{"model": "opus"}', encoding="utf-8")
    assert scan([tmp_path]) == []


# ---------------------------------------------------------------- output


def test_report_lists_unpinned_first_and_names_the_cost_question(tmp_path):
    write_agent(tmp_path, "pinned.md", "---\nname: pinned\nmodel: haiku\n---\n")
    write_agent(tmp_path, "loose.md", "---\nname: loose\n---\n")
    report = format_report(scan([tmp_path]), [tmp_path])
    assert report.index("Unpinned") < report.index("Pinned")
    assert "loose" in report
    assert "routing_audit" in report


def test_report_says_so_when_everything_is_pinned(tmp_path):
    write_agent(tmp_path, "a.md", "---\nname: a\nmodel: opus\n---\n")
    report = format_report(scan([tmp_path]), [tmp_path])
    assert "Every definition pins a model" in report
    assert "routing_audit" not in report


def test_report_handles_no_definitions_at_all(tmp_path):
    assert "No agent definitions found" in format_report([], [tmp_path])


def test_format_report_survives_a_definition_with_an_empty_name():
    """``max(len(name))`` is used for column width; an empty name must not
    make the formatter raise."""
    agent = AgentDef(path=Path("a.md"), name="", model=None)
    assert "a.md" in format_report([agent], [Path(".")])


# ---------------------------------------------------------------- exit codes


def test_exit_code_is_1_when_something_is_unpinned(tmp_path, capsys):
    write_agent(tmp_path, "loose.md", "---\nname: loose\n---\n")
    assert main([str(tmp_path)]) == 1
    assert "loose" in capsys.readouterr().out


def test_exit_code_is_0_when_all_pinned(tmp_path, capsys):
    write_agent(tmp_path, "a.md", "---\nname: a\nmodel: opus\n---\n")
    assert main([str(tmp_path)]) == 0
    capsys.readouterr()


def test_explicit_inherit_alone_does_not_fail_the_lint(tmp_path, capsys):
    """Inheriting on purpose is a decision, and the lint does not second-guess
    decisions — it only reports fields nobody filled in."""
    write_agent(tmp_path, "a.md", "---\nname: a\nmodel: inherit\n---\n")
    assert main([str(tmp_path)]) == 0
    capsys.readouterr()


def test_malformed_only_fails_under_strict(tmp_path, capsys):
    write_agent(tmp_path, "notes.md", "# not an agent\n")
    assert main([str(tmp_path)]) == 0
    assert main([str(tmp_path), "--strict"]) == 1
    capsys.readouterr()


def test_json_output_is_parseable_and_carries_the_states(tmp_path, capsys):
    import json

    write_agent(tmp_path, "loose.md", "---\nname: loose\n---\n")
    write_agent(tmp_path, "tight.md", "---\nname: tight\nmodel: sonnet\n---\n")
    main([str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["unpinned"] == 1
    assert {a["name"]: a["state"] for a in payload["agents"]} == {
        "loose": "unpinned",
        "tight": "pinned",
    }


# ---------------------------------------------------------------- privacy


def test_nothing_but_name_model_and_path_leaves_the_file(tmp_path, capsys):
    """The lint's whole claim is that it reads frontmatter and nothing else.
    A description and a body are the two places private content lives, so
    neither may appear in any output mode."""
    write_agent(
        tmp_path,
        "a.md",
        "---\n"
        "name: a\n"
        "description: SECRET-DESCRIPTION internal client name\n"
        "---\n"
        "SECRET-BODY the actual prompt\n",
    )
    main([str(tmp_path)])
    text_out = capsys.readouterr().out
    main([str(tmp_path), "--json"])
    json_out = capsys.readouterr().out
    for blob in (text_out, json_out):
        assert "SECRET-DESCRIPTION" not in blob
        assert "SECRET-BODY" not in blob
