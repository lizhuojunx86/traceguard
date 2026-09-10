"""Every CLI --help must render, for every subcommand.

argparse runs help strings through %-formatting, so a bare `%` raises
ValueError. Until 3.13 that surfaced only when someone actually asked for help;
from 3.14 on ``add_parser`` validates the string eagerly, so a bad help string
stops the CLI being constructed at all: on 3.14 EVERY ``sources`` subcommand
died, ``enable`` included, because all three ``add_parser`` calls run before
``parse_args``. Only four tests failed there — they happened to be the ones
touching ``list`` and ``drift`` — so the test count understated the damage.

The bug that prompted this ("with a Wilson 95% CI") sat in a shipped
subcommand, and no test anywhere touched ``--help``, which is how a
1000-test suite stayed green over a CLI that could not print its own usage.
"""
from __future__ import annotations

import argparse
import io
import re
from contextlib import redirect_stdout

import pytest

from traceguard.audit import __main__ as audit_cli
from traceguard.sources import __main__ as sources_cli

CLIS = [
    pytest.param(sources_cli, id="traceguard.sources"),
    pytest.param(audit_cli, id="traceguard.audit"),
]

#: argparse's own specifiers, the only bare `%` that is legal in a help string.
_ARGPARSE_SPECIFIER = re.compile(r"%\((?:prog|default|type|choices|dest|metavar|const)\)[sdrfg]")


def _parsers_of(module) -> list[argparse.ArgumentParser]:
    """Every parser the module builds, captured as it builds them.

    These CLIs construct their parser inside ``main()``, so there is no
    module-level object to introspect. Capturing the real one keeps the test
    honest about what actually ships rather than about a copy that can drift.
    """
    seen: list[argparse.ArgumentParser] = []
    original_parse = argparse.ArgumentParser.parse_args
    original_init = argparse.ArgumentParser.__init__

    def record_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        seen.append(self)

    argparse.ArgumentParser.__init__ = record_init
    try:
        with redirect_stdout(io.StringIO()), pytest.raises(SystemExit):
            module.main(["--help"])
    finally:
        argparse.ArgumentParser.__init__ = original_init
        argparse.ArgumentParser.parse_args = original_parse
    return seen


def _subcommands(module) -> list[str]:
    names: list[str] = []
    for parser in _parsers_of(module):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                names.extend(action.choices)
    return sorted(set(names))


@pytest.mark.parametrize("module", CLIS)
def test_top_level_help_renders_and_exits_0(module, capsys):
    with pytest.raises(SystemExit) as exc:
        module.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out, f"{module.__name__} --help printed no usage"


@pytest.mark.parametrize("module", CLIS)
def test_every_subcommand_help_renders_and_exits_0(module, capsys):
    """Discovered from the parser, so a subcommand added later is covered the
    day it lands — the drift subcommand shipped without any --help coverage."""
    names = _subcommands(module)
    assert names, f"no subcommands discovered for {module.__name__}"
    capsys.readouterr()  # drop the discovery run's output

    for name in names:
        with pytest.raises(SystemExit) as exc:
            module.main([name, "--help"])
        assert exc.value.code == 0, f"{module.__name__} {name} --help exited {exc.value.code}"
        out = capsys.readouterr().out
        assert "usage:" in out, f"{module.__name__} {name} --help printed no usage"


def test_the_literal_percents_survive_formatting(capsys):
    """The exact regression, both halves of it."""
    with pytest.raises(SystemExit):
        sources_cli.main(["--help"])
    assert "95% CI" in capsys.readouterr().out  # the subcommand listing

    with pytest.raises(SystemExit):
        sources_cli.main(["drift", "--help"])
    # argparse wraps, so match the unwrapped fragment: the point is that `%%`
    # in the source renders as one literal `%` rather than raising.
    assert "use % as the" in capsys.readouterr().out


@pytest.mark.parametrize("module", CLIS)
def test_no_help_string_carries_an_unescaped_percent(module):
    """Static guard, so the next one is caught at authoring time on any Python.

    A literal percent must be written ``%%``; the only legal bare form is one
    of argparse's own ``%(default)s``-style specifiers.
    """
    offenders = []
    for parser in _parsers_of(module):
        for action in parser._actions:
            for text in (action.help, getattr(parser, "description", None)):
                if not text:
                    continue
                stripped = _ARGPARSE_SPECIFIER.sub("", text).replace("%%", "")
                if "%" in stripped:
                    offenders.append(text)
    assert not offenders, (
        f"{module.__name__}: unescaped % in help string(s) {offenders} — write a literal "
        "percent as %%, or argparse raises ValueError (and on Python 3.14 the CLI will "
        "not even build)"
    )
