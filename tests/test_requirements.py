"""Requirement records: validation, canonical order and derived criteria."""

import copy
import itertools

import pytest

from ticket_pipeline.lib import ledger, requirements

A = {"id": "REQ-1", "text": "first"}
B = {"id": "REQ-2", "text": "second", "source": "SA-659"}
C = {"id": "req-1", "text": "case differs from REQ-1"}


@pytest.mark.parametrize(
    "items",
    [
        None,
        "REQ-1",
        {},
        [],
        ["REQ-1"],
        [["id", "REQ-1"]],
        [{"id": "REQ-1", "text": "x", "waived": True}],
        [{"id": "REQ-1", "text": "x", "status": "optional"}],
        [{"text": "no id"}],
        [{"id": 1, "text": "numeric id"}],
        [{"id": "", "text": "x"}],
        [{"id": "   ", "text": "x"}],
        [{"id": " REQ-1", "text": "x"}],
        [{"id": "REQ-1 ", "text": "x"}],
        [{"id": "REQ 1", "text": "x"}],
        [{"id": "-REQ", "text": "x"}],
        [{"id": "REQ-1\n", "text": "x"}],
        [{"id": "REQ-1"}],
        [{"id": "REQ-1", "text": ""}],
        [{"id": "REQ-1", "text": "   "}],
        [{"id": "REQ-1", "text": 5}],
        [{"id": "REQ-1", "text": "x", "source": ""}],
        [{"id": "REQ-1", "text": "x", "source": "  "}],
        [{"id": "REQ-1", "text": "x", "source": 3}],
        [A, {"id": "REQ-1", "text": "different text, same id"}],
        [A, copy.deepcopy(A)],
    ],
)
def test_invalid_collections_are_rejected(items):
    with pytest.raises(ValueError):
        requirements.validate(items)
    with pytest.raises(ValueError):
        requirements.canonical(items)
    with pytest.raises(ValueError):
        requirements.derived_criteria(items)


def test_error_messages_name_the_offending_record():
    with pytest.raises(ValueError, match=r"requirements\[1\]\.id"):
        requirements.validate([A, {"id": "bad id", "text": "x"}])
    with pytest.raises(ValueError, match="Duplicate requirement id: REQ-1"):
        requirements.validate([A, A])


def test_valid_collection_passes_and_ids_are_case_distinct():
    assert requirements.validate([A, B, C]) is None
    assert [r["id"] for r in requirements.canonical([B, C, A])] == ["REQ-1", "REQ-2", "req-1"]


def test_canonical_is_independent_of_input_order():
    expected = requirements.canonical([A, B, C])
    for permutation in itertools.permutations([A, B, C]):
        assert requirements.canonical(list(permutation)) == expected
        assert ledger.digest(requirements.canonical(list(permutation))) == ledger.digest(expected)


def test_canonical_preserves_fields_as_supplied_and_does_not_mutate_input():
    padded = {"id": "REQ-9", "text": "  padded text ", "source": " SA-1 "}
    items = [padded, A]
    before = copy.deepcopy(items)
    result = requirements.canonical(items)
    assert items == before
    assert result[1] == padded and result[1] is not padded
    result[0]["text"] = "changed"
    assert items == before
    assert "source" not in result[0]


def test_derived_criteria_follows_canonical_order():
    assert requirements.derived_criteria([B, A]) == ["first", "second"]


def test_ids_are_never_positional():
    # Identity is the id: moving a record never changes it, and no id is generated.
    assert [r["id"] for r in requirements.canonical([B, A])] == ["REQ-1", "REQ-2"]
    assert requirements.FIELDS == {"id", "text", "source"}
