"""The European legal-form suffixes added for the non-US feeds."""

from __future__ import annotations

import pytest

from stockbrain.instruments.normalize import instrument_name_key

NEW_SUFFIXES = (
    "gmbh",
    "kgaa",
    "sarl",
    "sas",
    "bv",
    "srl",
    "sl",
    "sau",
    "sca",
    "scs",
    "oy",
    "aps",
)


@pytest.mark.parametrize("suffix", NEW_SUFFIXES)
def test_a_new_legal_suffix_is_stripped(suffix: str) -> None:
    assert instrument_name_key(f"Acme {suffix}") == "acme"


def test_a_single_word_suffix_is_not_stripped_away() -> None:
    # The suffix loop only fires while more than one word remains.
    assert instrument_name_key("SL") == "sl"
    assert instrument_name_key("SAS") == "sas"
