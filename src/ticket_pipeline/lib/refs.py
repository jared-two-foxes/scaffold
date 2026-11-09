"""Artifact references: ``sha256:<64 hex>`` naming a blob stored in the feature's ledger."""

from __future__ import annotations

import re

REFERENCE = re.compile(r"sha256:[a-f0-9]{64}\Z")
COMMIT = re.compile(r"[a-f0-9]{40}\Z")


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def is_reference(value: object) -> bool:
    return isinstance(value, str) and bool(REFERENCE.fullmatch(value))


def is_commit(value: object) -> bool:
    return isinstance(value, str) and bool(COMMIT.fullmatch(value))


def artifact_name(reference: str) -> str:
    require(is_reference(reference), "Expected an artifact reference: sha256:<64 hex characters>")
    return reference.removeprefix("sha256:")
