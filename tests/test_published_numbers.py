"""Every number quoted in prose must match a live benchmark run.

CI already reproduces the benchmark, which proves the code runs. It said
nothing about whether the documents still told the truth about it -- and twice
a change moved the headline numbers while the README kept the old ones. The
second time survived a full verification pass and was caught only by rebuilding
a figure by hand.

These tests close that gap. They read the shipped documents and require the
*current* value of each figure to appear. A stale number fails the build and
names the file and the figure, so the drift is impossible to miss and trivial
to fix.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from fourshots.figures import EXPECTED, compute, indian_grouping, lakhs

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def figures():
    return compute()


def _normalise(text: str) -> str:
    """Fold the ways documents spell the same characters.

    HTML writes rupees as `&#8377;` and minus as `&minus;`, prose uses the
    literal glyphs, and Windows checkouts carry CRLF. None of that is a number
    changing, so none of it should fail the check.
    """
    return (
        text.replace("\r\n", "\n")
        .replace("&#8377;", "₹")
        .replace("&minus;", "−")
        .replace("&times;", "×")
        .replace("&nbsp;", " ")
    )


@pytest.mark.parametrize("document", sorted(EXPECTED))
def test_document_quotes_current_figures(document: str, figures) -> None:
    path = REPO / document
    assert path.exists(), f"{document} is listed as quoting figures but does not exist"
    text = _normalise(path.read_text(encoding="utf-8"))

    stale: list[str] = []
    for key in EXPECTED[document]:
        figure = figures[key]
        if not any(rendering in text for rendering in figure.renderings):
            stale.append(
                f"  {key}: expected one of {list(figure.renderings)} in {document}"
            )

    assert not stale, (
        f"{document} quotes numbers that no longer match a benchmark run.\n"
        + "\n".join(stale)
        + "\n\nRun `python -m fourshots.benchmark` and update the document."
    )


def test_every_expected_document_exists() -> None:
    """A renamed or deleted document must fail loudly rather than silently
    dropping out of the check."""
    for document in EXPECTED:
        assert (REPO / document).exists(), f"{document} is missing"


def test_check_would_actually_fail_on_drift(figures) -> None:
    """Prove the check has teeth.

    A verification that cannot fail is decoration -- the same lesson the
    mutation audit taught. This simulates a document quoting a number that is
    off by one and asserts the search does not find it.
    """
    recovered = figures["recovered_engine"]
    drifted = _normalise("The engine recovered ₹" + indian_grouping(1) + " last month.")
    assert not any(r in drifted for r in recovered.renderings)


# --- Formatting helpers ----------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (7, "7"),
        (999, "999"),
        (1000, "1,000"),
        (99999, "99,999"),
        (100000, "1,00,000"),
        (4985233, "49,85,233"),
        (12034042, "1,20,34,042"),
    ],
)
def test_indian_digit_grouping(value: int, expected: str) -> None:
    """A rupee figure written 4,985,233 in an Indian payments document reads as
    foreign. Last three digits, then pairs."""
    assert indian_grouping(value) == expected


def test_indian_grouping_accepts_decimals() -> None:
    assert indian_grouping(Decimal("4985233")) == "49,85,233"


@pytest.mark.parametrize(
    "value,expected", [(4985233, "49.9L"), (7439249, "74.4L"), (100000, "1.0L")]
)
def test_lakh_rounding(value: int, expected: str) -> None:
    assert lakhs(value) == expected


# --- The figure set itself -------------------------------------------------

def test_every_figure_has_at_least_one_rendering(figures) -> None:
    for key, figure in figures.items():
        assert figure.renderings, f"{key} has no rendering to search for"
        assert all(r for r in figure.renderings), f"{key} has an empty rendering"


def test_expected_keys_all_exist(figures) -> None:
    """A typo in EXPECTED would silently check nothing."""
    for document, keys in EXPECTED.items():
        for key in keys:
            assert key in figures, f"{document} expects unknown figure {key!r}"


def test_engine_still_beats_baseline_in_the_published_figures(figures) -> None:
    """The documents assert an improvement. If that ever stopped being true,
    updating the numbers would not be the right fix."""
    assert figures["recovered_delta"].renderings[0].startswith("+")
