"""Delivery bookkeeping is append-only and never establishes independent verification."""

import copy

import pytest

from ticket_pipeline.lib.delivery import record


def feature():
    return {
        "objective": "Import quotes",
        "acceptance_criteria": ["Reject unknown status"],
        "constraints": ["Retain accepted statuses"],
        "tasks": {"import": {"status": "in_progress"}},
    }


def send(f, kind, **data):
    revision = f["tasks"]["import"].get("delivery", {}).get("revision", 0)
    record(f, "import", {"expected_revision": revision, "event": {"kind": kind, **data}})
    return f["tasks"]["import"]["delivery"]


def initialize(f, sequence=0):
    return send(
        f,
        "initialized",
        context={"original": "Reject unknown status", "updates": [], "obligations": []},
        head=None,
        sequence=sequence,
    )


def test_more_than_five_cycles_preserve_history_and_original_contract():
    f = feature()
    initialize(f)
    for n in range(1, 12):
        send(
            f,
            "begun",
            reference=f"intent-{n}",
            previous=None if n == 1 else f"receipt-{n - 1}",
            sequence=n,
        )
        j = send(f, "finished", reference=f"receipt-{n}", started=f"intent-{n}")
    assert j["sequence"] == 11
    assert len(j["history"]) == 23
    assert j["contract"]["acceptance_criteria"] == ["Reject unknown status"]
    assert f["tasks"]["import"]["status"] == "in_progress"


def test_cas_interruption_stale_receipt_and_reset_are_rejected():
    f = feature()
    initialize(f, sequence=5)
    with pytest.raises(ValueError, match="Stale"):
        record(f, "import", {"expected_revision": 0, "event": {"kind": "paused"}})
    send(f, "begun", reference="intent-6", previous=None, sequence=6)
    with pytest.raises(ValueError, match="already reserved"):
        send(f, "begun", reference="intent-7", previous=None, sequence=7)
    with pytest.raises(ValueError, match="matching"):
        send(f, "finished", reference="receipt-6", started="wrong-intent")
    send(f, "finished", reference="receipt-6", started="intent-6")
    with pytest.raises(ValueError, match="never reset"):
        initialize(f)


def test_context_is_additive_and_proposed_addressed_does_not_complete_issue():
    f = feature()
    j = initialize(f)
    c = copy.deepcopy(j["context"])
    update = {"kind": "add_obligation", "text": "Add status regression", "reason": "Review finding"}
    c["updates"].append(update)
    c["obligations"].append({"id": "status-regression", "text": "Add status regression"})
    send(f, "context", updates=[update], context=c)
    with pytest.raises(ValueError, match="cannot disappear"):
        send(f, "context", updates=[], context={**c, "obligations": []})
    with pytest.raises(ValueError, match="cannot be replaced"):
        send(f, "context", updates=[], context={**c, "original": "Allow any status"})
    with pytest.raises(ValueError, match="removal"):
        send(
            f,
            "context",
            updates=[{"kind": "remove_obligation", "reason": "Too difficult"}],
            context=c,
        )
    assert f["tasks"]["import"]["status"] == "in_progress"
