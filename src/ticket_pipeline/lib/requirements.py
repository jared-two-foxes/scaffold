"""Canonical feature requirements: the {id, text, source?} record, validation and ordering.

A requirement is identified by its ``id`` alone. Ids are chosen by the author, never derived
from list position, and are unique within a feature instance. Every requirement is mandatory:
there is deliberately no waiver, optional or status field. The canonical form is the records
sorted by id, so the order in which a caller supplies them can never change identity or any
digest computed over the canonical form.

Pure functions only: no ledger or state I/O, and inputs are never mutated.
"""

from __future__ import annotations

import copy
import re

ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
FIELDS = frozenset({"id", "text", "source"})


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate(items: object) -> None:
    """Reject a malformed requirements collection; return nothing when it is valid."""
    _require(isinstance(items, list) and bool(items), "requirements must be a nonempty array")
    seen: set[str] = set()
    for index, record in enumerate(items):
        where = f"requirements[{index}]"
        _require(isinstance(record, dict), f"{where} must be an object")
        unknown = sorted(str(key) for key in record.keys() - FIELDS)
        _require(not unknown, f"{where} has unknown fields: {', '.join(unknown)}")
        identifier = record.get("id")
        _require(
            isinstance(identifier, str) and ID_PATTERN.fullmatch(identifier) is not None,
            f"{where}.id must match [A-Za-z0-9][A-Za-z0-9._-]*",
        )
        _require(_is_text(record.get("text")), f"{where}.text must be a nonempty string")
        _require(
            "source" not in record or _is_text(record["source"]),
            f"{where}.source must be a nonempty string when present",
        )
        _require(identifier not in seen, f"Duplicate requirement id: {identifier}")
        seen.add(identifier)


def canonical(items: object) -> list[dict]:
    """A validated copy of the requirements sorted by id (input order never matters)."""
    validate(items)
    return sorted((copy.deepcopy(record) for record in items), key=lambda record: record["id"])


def derived_criteria(items: object) -> list[str]:
    """Requirement texts in canonical order: the criteria projection of the root contract."""
    return [record["text"] for record in canonical(items)]
