"""Tests for configuration loading.

Small surface, but it handles secrets: a silently-empty webhook secret would
turn signature verification into a formality while still looking like it works.
"""

import pytest

from fourshots.config import load_env, optional, require


def test_loads_key_value_pairs(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("FOURSHOTS_TEST_KEY=value123\n", encoding="utf-8")
    monkeypatch.delenv("FOURSHOTS_TEST_KEY", raising=False)

    load_env(env)
    assert require("FOURSHOTS_TEST_KEY") == "value123"


def test_real_environment_wins_over_the_file(tmp_path, monkeypatch) -> None:
    """A deployment must be able to override the file without editing it."""
    env = tmp_path / ".env"
    env.write_text("FOURSHOTS_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("FOURSHOTS_TEST_KEY", "from_environment")

    load_env(env)
    assert require("FOURSHOTS_TEST_KEY") == "from_environment"


def test_comments_and_blank_lines_are_skipped(tmp_path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n\nFOURSHOTS_TEST_KEY=value\n   \nnot_a_pair\n", encoding="utf-8"
    )
    monkeypatch.delenv("FOURSHOTS_TEST_KEY", raising=False)

    load_env(env)
    assert require("FOURSHOTS_TEST_KEY") == "value"


def test_missing_file_is_not_an_error(tmp_path) -> None:
    load_env(tmp_path / "does_not_exist")  # must not raise


def test_missing_required_setting_fails_loudly(monkeypatch) -> None:
    """Better to refuse to start than to run with an empty secret."""
    monkeypatch.delenv("FOURSHOTS_ABSENT", raising=False)
    with pytest.raises(RuntimeError, match="FOURSHOTS_ABSENT"):
        require("FOURSHOTS_ABSENT")


def test_blank_value_counts_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("FOURSHOTS_BLANK", "   ")
    with pytest.raises(RuntimeError):
        require("FOURSHOTS_BLANK")


def test_optional_returns_its_default(monkeypatch) -> None:
    monkeypatch.delenv("FOURSHOTS_ABSENT", raising=False)
    assert optional("FOURSHOTS_ABSENT", "fallback") == "fallback"
