"""Tests for the translation files."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "hass_energy" / "translations"
)
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """Every string in the file by its dotted key."""
    strings: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            strings |= _flatten(value, f"{prefix}{key}.")
        else:
            strings[f"{prefix}{key}"] = value
    return strings


def _load(path: Path) -> dict[str, str]:
    return _flatten(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "path",
    [path for path in TRANSLATIONS.glob("*.json") if path.name != "en.json"],
    ids=lambda path: path.stem,
)
def test_translation_matches_english(path: Path) -> None:
    """Every language has English's strings, with the same placeholders."""
    english = _load(TRANSLATIONS / "en.json")
    strings = _load(path)

    assert strings.keys() == english.keys()
    for key, text in english.items():
        assert set(PLACEHOLDER.findall(strings[key])) == set(
            PLACEHOLDER.findall(text)
        ), key
