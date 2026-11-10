"""A nature code the model put in the wrong column must not pass in silence.

Compact output is a positional array, so every column is trusted by position and
nothing cross-checks the value against the column that holds it. Adding the eighth
column (extent) made gpt-4o-mini write the extent code into column 3, and
``expand_nature`` mapped it to ``"unknown"`` without raising or warning. Geo search
filters event-role mentions on ``nature in ('primary','secondary')``, so the mention
quietly dropped out of the searches it should have answered.

The vocabularies make this detectable with certainty rather than by guesswork: every
nature code is one letter and every other compact code column uses two, so the four
sets are disjoint. These tests hold that invariant, prove the warning fires on the
literal row the model returned, and prove it stays quiet on a good row.
"""

from __future__ import annotations

import logging

import pytest
from agate_nodes.place_extract.compact_array_parse import row_to_entry
from agate_nodes.place_extract.compact_codes import (
    ADDRESS_PLACE_KIND_TO_CODE,
    EXTENT_TO_CODE,
    LOCATION_TYPE_TO_CODE,
    NATURE_TO_CODE,
    expand_nature,
    nature_code_slot_owner,
)
from agate_nodes.place_extract.compact_expand import expand_compact_entry
from agate_nodes.place_extract.compact_prompt import COMPACT_OUTPUT_INSTRUCTIONS

# The exact row gpt-4o-mini returned on 2026-11-10, temperature 0, for a flood
# warning covering a whole county. Column 3 should be "p"; it holds the extent code.
CORRUPTED_ROW = [
    "Howard County, MD",
    "cn",
    "cx",
    "",
    "A flood warning covers all of Howard County due to expected heavy rainfall.",
    "",
    "Flood warning covers all of Howard County",
    "cx",
]

# The same row as it should have come back. This is the negative control: the
# warning must not fire here, or it proves nothing when it fires above.
GOOD_ROW = ["Howard County, MD", "cn", "p", "", "A flood warning.", "", "Flood warning", "cx"]

ARTICLE = "A flood warning covers all of Howard County through Tuesday night."


# --- the invariant the detection rests on -----------------------------------


def test_nature_codes_are_one_letter_and_the_others_are_two() -> None:
    assert all(len(code) == 1 for code in NATURE_TO_CODE.values())
    for vocabulary in (LOCATION_TYPE_TO_CODE, ADDRESS_PLACE_KIND_TO_CODE, EXTENT_TO_CODE):
        assert all(len(code) == 2 for code in vocabulary.values())


def test_no_code_is_shared_between_the_nature_column_and_any_other() -> None:
    natures = set(NATURE_TO_CODE.values())
    for vocabulary in (LOCATION_TYPE_TO_CODE, ADDRESS_PLACE_KIND_TO_CODE, EXTENT_TO_CODE):
        assert natures.isdisjoint(set(vocabulary.values()))


@pytest.mark.parametrize(
    ("token", "owner"),
    [
        ("cx", "extent (column 8)"),
        ("wi", "extent (column 8)"),
        ("pn", "address_place_kind (column 4)"),
        ("pv", "address_place_kind (column 4)"),
        ("cn", "type (column 2)"),
        ("ih", "type (column 2)"),
        ("zz", ""),
        ("p", ""),
    ],
)
def test_a_non_nature_token_is_attributed_to_the_column_that_owns_it(
    token: str, owner: str
) -> None:
    assert nature_code_slot_owner(token) == owner


# --- the signal --------------------------------------------------------------


def test_the_mis_slotted_code_still_becomes_unknown(caplog: pytest.LogCaptureFixture) -> None:
    """The value is reported, never repaired: moving it would guess at the rest of the row."""
    with caplog.at_level(logging.WARNING):
        assert expand_nature("cx", location="Howard County, MD") == "unknown"


def test_the_warning_fires_on_the_row_the_model_actually_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        entry = row_to_entry(CORRUPTED_ROW)
    assert entry["nature"] == "unknown"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "'cx'" in warnings[0]
    assert "Howard County, MD" in warnings[0]
    assert "extent (column 8)" in warnings[0]


def test_the_warning_stays_quiet_on_the_same_row_done_right(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        entry = row_to_entry(GOOD_ROW)
    assert entry["nature"] == "primary"
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_an_object_row_is_covered_too(caplog: pytest.LogCaptureFixture) -> None:
    """node_port hands dict rows straight to expand_compact_entry, skipping row_to_entry."""
    with caplog.at_level(logging.WARNING):
        out = expand_compact_entry(
            ARTICLE,
            {"location": "Howard County, MD", "type": "county", "nature": "cx"},
        )
    assert out["nature"] == "unknown"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "Howard County, MD" in warnings[0]


def test_expanding_an_already_expanded_row_does_not_warn_twice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """row_to_entry runs first, then expand_compact_entry; 'unknown' is valid, so it is quiet."""
    entry = row_to_entry(CORRUPTED_ROW)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        expand_compact_entry(ARTICLE, entry)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


# --- the prompt side ---------------------------------------------------------


def test_the_prompt_states_the_rule_the_decoder_checks() -> None:
    """Measured on gpt-4o-mini: 16 bad nature codes in 138 rows before this rule,
    1 in 193 rows after it, pooled over every post-fix run. Reduced, not eliminated,
    which is why the warning above matters on its own.
    """
    assert "Column 3 (nature) is ALWAYS one single letter" in COMPACT_OUTPUT_INSTRUCTIONS
    assert "never a two-letter code" in COMPACT_OUTPUT_INSTRUCTIONS
