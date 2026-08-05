"""Pin the two published `epsActual` revision numbers to the shipped dataset.

The README, the case studies and tg-attest's README all quote 41.4% / 15.3%.
These tests fail if the committed dataset stops supporting them, which is the
only way a silent drift between prose and evidence gets caught.

The dataset is a frozen capture of a past window, not a computed parameter, so
the expectations are exact counts rather than tolerances. If a rebuild ever
changes them legitimately, the prose has to change with it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "analysis"
DATA = ANALYSIS / "data"


def _load_module():
    spec = importlib.util.spec_from_file_location("eps_revision", ANALYSIS / "eps_revision.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["eps_revision"] = module
    spec.loader.exec_module(module)
    return module


eps_revision = _load_module()


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((DATA / "manifest.json").read_text())


@pytest.fixture(scope="module")
def qt_rows() -> list[dict]:
    return eps_revision.load_rows(DATA / "eps_revision_qt_pit_2026h1.csv")


@pytest.fixture(scope="module")
def fp_rows() -> list[dict]:
    return eps_revision.load_rows(DATA / "eps_revision_forward_poll_2026h2.csv")


def test_headline_difference_rate(qt_rows):
    s = eps_revision.summarise(qt_rows)
    assert (s["differs"], s["n"]) == (896, 2163)
    assert round(100 * s["differs"] / s["n"], 1) == 41.4


def test_headline_flip_rate(qt_rows):
    s = eps_revision.summarise(qt_rows)
    assert (s["flipped"], s["n"]) == (332, 2163)
    assert round(100 * s["flipped"] / s["n"], 1) == 15.3


def test_forward_poll_replication(fp_rows):
    """The second capture is materially lower. That is documented, not a bug."""
    s = eps_revision.summarise(fp_rows)
    assert (s["differs"], s["flipped"], s["n"]) == (1087, 268, 5850)


def test_confidence_intervals_bracket_the_headline(qt_rows):
    s = eps_revision.summarise(qt_rows)
    lo, hi = s["differs_ci"]
    assert lo < 0.414 < hi
    lo, hi = s["flipped_ci"]
    assert lo < 0.153 < hi


@pytest.mark.parametrize(
    "name", ["eps_revision_qt_pit_2026h1.csv", "eps_revision_forward_poll_2026h2.csv"]
)
def test_rows_are_internally_consistent(name):
    """Every published flag must agree with the digest pair it summarises."""
    assert eps_revision.audit_rows(eps_revision.load_rows(DATA / name)) == []


def test_manifest_matches_the_shipped_files(manifest):
    import hashlib

    for ds in manifest["datasets"]:
        path = DATA / ds["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == ds["sha256"], ds["file"]
        assert len(eps_revision.load_rows(path)) == ds["rows"], ds["file"]


def test_no_vendor_values_are_published():
    """The ToS line. A regression here is a licence problem, not a test failure."""
    forbidden = {
        "eps_actual",
        "eps_estimated",
        "epsActual",
        "epsEstimated",
        "revenue_actual",
        "revenue_estimated",
        "num_analysts_eps",
        "grades_count",
        "last_updated",
    }
    for path in DATA.glob("*.csv"):
        header = path.read_text().splitlines()[0].split(",")
        assert forbidden.isdisjoint(header), path.name


# ── ToS containment ─────────────────────────────────────────────────────────
#
# A licence breach is irreversible in a way a failing assertion is not: once a
# vendor value is pushed to a public remote it has been redistributed, and no
# later commit un-publishes it. So this block does not ask "did a forbidden
# column NAME appear" — a name is cosmetic and an EPS value smuggled into a
# column called `notes` passes that check. It asserts the published shape
# positively: exactly these columns, each drawn from a closed vocabulary, and
# no cell anywhere that can be read as a number.

PUBLISHED_COLUMNS = [
    "dataset",
    "symbol",
    "period",
    "first_seen_date",
    "final_ref_date",
    "router",
    "first_seen_hash",
    "final_hash",
    "eps_differs",
    "direction",
    "magnitude_bucket",
    "first_seen_tradeable",
    "final_tradeable",
    "decision_flipped",
    "surprise_sign_flipped",
    "first_seen_stale",
]

CSV_NAMES = ["eps_revision_qt_pit_2026h1.csv", "eps_revision_forward_poll_2026h2.csv"]

# A bare decimal: "0.42", "-1.07", "2034.97". Vendor EPS values, estimates and
# revenue figures all take this form. `magnitude_bucket` labels ("[0.25,1.00)",
# ">=1.00") deliberately do NOT, because they carry brackets — a bucket edge is
# a published constant, an unadorned number in a cell is a leak.
BARE_NUMBER = re.compile(r"^[+-]?\d+\.\d+$")

# Anything that looks like a two-decimal quantity, wherever it sits inside a
# cell. Stricter than BARE_NUMBER and aimed at the smuggling case: a value
# glued into a longer string, e.g. "AAPL:1.42" or "delta=-0.07".
EMBEDDED_2DP = re.compile(r"(?<![\d.])[+-]?\d+\.\d{2}(?![\d])")

CLOSED_VOCABULARIES = {
    "dataset": {"qt_pit_2026h1", "forward_poll_2026h2"},
    "router": {"V3", "V5"},
    "direction": {"up", "down", "none"},
    "magnitude_bucket": {"0", "(0,0.01)", "[0.01,0.05)", "[0.05,0.25)", "[0.25,1.00)", ">=1.00"},
    "eps_differs": {"true", "false"},
    "decision_flipped": {"true", "false"},
    "first_seen_tradeable": {"true", "false", ""},
    "final_tradeable": {"true", "false", ""},
    "surprise_sign_flipped": {"true", "false", ""},
    "first_seen_stale": {"true", "false", ""},
}

HEX16 = re.compile(r"^[0-9a-f]{16}$")


@pytest.mark.parametrize("name", CSV_NAMES)
def test_published_schema_is_exactly_the_allowlist(name):
    """A rebuild may not add a column without this test being updated first."""
    header = (DATA / name).read_text().splitlines()[0].split(",")
    assert header == PUBLISHED_COLUMNS, name


@pytest.mark.parametrize("name", CSV_NAMES)
def test_no_published_cell_can_be_read_as_a_number(name):
    """The ToS line, enforced on VALUES rather than on column names.

    Absence of the field name `eps_actual` is not absence of the EPS. Every
    published cell must be a label, a date, a symbol, a boolean or a digest —
    never a quantity.
    """
    offenders = []
    for i, row in enumerate(eps_revision.load_rows(DATA / name), start=2):
        for col, cell in row.items():
            if col == "magnitude_bucket":
                continue  # bracketed bucket labels; covered by its vocabulary
            if BARE_NUMBER.match(cell) or EMBEDDED_2DP.search(cell):
                offenders.append(f"{name}:{i} {col}={cell!r}")
    assert offenders == [], offenders[:20]


@pytest.mark.parametrize("name", CSV_NAMES)
def test_categorical_columns_draw_from_closed_vocabularies(name):
    """Nothing free-form ships, so nothing free-form can carry a value."""
    seen = {col: set() for col in CLOSED_VOCABULARIES}
    for row in eps_revision.load_rows(DATA / name):
        for col in CLOSED_VOCABULARIES:
            seen[col].add(row[col])
    for col, allowed in CLOSED_VOCABULARIES.items():
        assert seen[col] <= allowed, (name, col, sorted(seen[col] - allowed))


@pytest.mark.parametrize("name", CSV_NAMES)
def test_digest_columns_are_opaque_and_fixed_width(name):
    """16 lowercase hex or empty — no room to hide a value in a digest column."""
    for i, row in enumerate(eps_revision.load_rows(DATA / name), start=2):
        for col in ("first_seen_hash", "final_hash"):
            cell = row[col]
            assert cell == "" or HEX16.match(cell), f"{name}:{i} {col}={cell!r}"


# Numbers the manifest is allowed to state. All are analysis parameters or
# aggregate counts chosen by us; none is sourced from the vendor feed.
MANIFEST_NUMERIC_KEYS = {
    "schema_version",
    "threshold_v3",
    "threshold_v5",
    "plausible_abs_eps",
    "rows",
    "flipped",
    "rate",
}


def _numeric_leaves(node, key=None, path="$"):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _numeric_leaves(v, k, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _numeric_leaves(v, key, f"{path}[{i}]")
    elif isinstance(node, (int, float)) and not isinstance(node, bool):
        yield path, key, node


def test_manifest_states_no_unallowlisted_number(manifest):
    """Same rule as the CSVs, applied to the manifest's own numeric leaves."""
    offenders = [
        f"{path} = {value}"
        for path, key, value in _numeric_leaves(manifest)
        if key not in MANIFEST_NUMERIC_KEYS
    ]
    assert offenders == [], offenders


def test_manifest_prose_embeds_no_two_decimal_quantity(manifest):
    """A vendor value pasted into a description string would also be a breach."""
    offenders = []

    def walk(node, path="$"):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str) and EMBEDDED_2DP.search(node):
            offenders.append(f"{path} = {node!r}")

    walk(manifest)
    assert offenders == [], offenders


def test_manifest_still_declares_every_withheld_field(manifest):
    """The withheld list is part of the licence position; it may not shrink."""
    assert {
        "eps_actual",
        "eps_estimated",
        "revenue_actual",
        "revenue_estimated",
        "num_analysts_eps",
        "grades_count",
        "lastUpdated",
    } <= set(manifest["not_published"])


def test_only_the_reviewed_files_ship_under_data():
    """A stray artifact under data/ is an unreviewed publication."""
    actual = {p.name for p in DATA.iterdir() if p.is_file()}
    assert actual == {*CSV_NAMES, "manifest.json"}, sorted(actual)


def test_the_pepper_is_not_in_the_repo():
    """The HMAC key must never be committed; see analysis/README.md."""
    strays = [
        str(p.relative_to(ROOT))
        for p in ROOT.rglob("*pepper*")
        if p.is_file() and ".git/" not in str(p)
    ]
    assert strays == [], strays


# ── the documented self-check must be a command that runs ───────────────────
#
# Same pattern as tg-attest's tests/test_readme_repro.py: read the command OUT
# of the document and execute it, rather than copying it into the test. A copy
# goes stale the first time the doc is edited and the test stays green.
#
# This one earns its keep. The commitment is SHA-256 over the pepper's 32
# DECODED bytes; the file on disk holds 64 ASCII hex characters plus a newline,
# so `shasum -a 256 pepper` returns a perfectly well-formed digest that is not
# the committed value. Documenting the wrong command would send an auditor to
# the conclusion that the key is corrupt.

README_MD = ANALYSIS / "README.md"
SELF_CHECK = re.compile(r"^\$ (python3 -c \".+\")$", re.MULTILINE)
PEPPER_PATH = Path.home() / ".local/share/traceguard-eps-disclosure/pepper"

# Skipped rather than passed when the key is absent, which is the normal state
# in CI: the pepper is deliberately not available to any automated runner. A
# silent pass here would mean the one check that closes the loop between the
# document and the manifest never actually ran anywhere.
needs_pepper = pytest.mark.skipif(
    not PEPPER_PATH.exists(),
    reason=(
        f"pepper not present at {PEPPER_PATH} — expected in CI, which holds no "
        "key material. This assertion only has meaning on the data holder's "
        "machine; run the suite there before publishing a rebuild."
    ),
)


def _extracted_self_check() -> str:
    m = SELF_CHECK.search(README_MD.read_text())
    assert m, 'analysis/README.md no longer contains a `$ python3 -c "..."` self-check'
    return m.group(1)


def test_the_self_check_command_hashes_the_decoded_key_not_the_file_bytes():
    """Runs everywhere, including CI, because it needs no key.

    Guards the actual trap statically: the command must hex-decode before
    hashing, and must not be a plain digest of the file.
    """
    cmd = _extracted_self_check()
    assert "bytes.fromhex" in cmd, (
        "the self-check must hex-decode the key before hashing; hashing the "
        "file's raw bytes digests 64 ASCII characters plus a newline instead "
        "of the 32 bytes the manifest commits to"
    )
    assert ".strip()" in cmd, "the trailing newline must be stripped before decoding"
    assert "shasum" not in cmd and "sha256sum" not in cmd


def test_the_readme_documents_the_shasum_trap():
    """The wrong command is the one a reader reaches for first."""
    text = README_MD.read_text()
    assert "shasum" in text, "the README must name the shasum pitfall explicitly"
    assert "65" in text, "the README must say what shasum actually digests"


@needs_pepper
def test_the_self_check_command_actually_prints_the_manifest_commitment(manifest):
    """Execute the documented command verbatim and compare to the manifest."""
    import shlex
    import subprocess

    r = subprocess.run(
        shlex.split(_extracted_self_check()), capture_output=True, text=True, cwd=ROOT
    )
    assert r.returncode == 0, r.stderr
    assert (
        r.stdout.strip() == manifest["pepper_sha256"]
    ), "the command in analysis/README.md does not reproduce pepper_sha256"


@needs_pepper
def test_the_readme_shows_the_commitment_it_would_print(manifest):
    """The expected output pasted under the command must be the live value."""
    assert (
        manifest["pepper_sha256"] in README_MD.read_text()
    ), "analysis/README.md quotes a stale pepper_sha256"


@needs_pepper
def test_hashing_the_raw_file_gives_a_different_answer(manifest):
    """Negative control.

    Without this, the test above could be passing for the wrong reason. It also
    pins the exact failure mode: the raw-file digest is well-formed and wrong.
    """
    import hashlib

    raw = hashlib.sha256(PEPPER_PATH.read_bytes()).hexdigest()
    assert len(raw) == 64, "the wrong answer is still a well-formed digest"
    assert (
        raw != manifest["pepper_sha256"]
    ), "raw-file and decoded-key digests coincided; the trap this guards is gone"


def test_production_thresholds_are_not_the_flattering_choice(manifest):
    """2.0/10.0 are the live router's. Most neighbours give a HIGHER rate."""
    sweep = next(d for d in manifest["datasets"] if d["id"] == "qt_pit_2026h1")[
        "threshold_sensitivity"
    ]
    production = next(r for r in sweep if r["production"])
    assert (production["threshold_v3"], production["threshold_v5"]) == (2.0, 10.0)
    assert production["rate"] < max(r["rate"] for r in sweep)
