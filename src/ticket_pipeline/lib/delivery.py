"""Append-only delivery journal. Native callers authenticate evidence, not Scaffold."""

import copy
from datetime import UTC, datetime

ACTIONS = {"delivery-record"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def record(feature, task_id, payload):
    require(task_id in feature["tasks"], "Unknown delivery issue")
    task = feature["tasks"][task_id]
    require(
        isinstance(payload, dict) and set(payload) == {"expected_revision", "event"},
        "Delivery requires expected_revision and event",
    )
    journal = task.get("delivery")
    revision = journal["revision"] if journal else 0
    require(
        type(payload["expected_revision"]) is int and payload["expected_revision"] == revision,
        "Stale delivery revision",
    )
    e = copy.deepcopy(payload["event"])
    require(isinstance(e, dict) and isinstance(e.get("kind"), str), "Delivery event required")
    if journal is None:
        require(e["kind"] == "initialized", "Missing delivery history; initialize or migrate")
        require(set(e) == {"kind", "context", "head", "sequence"}, "Invalid initialization")
        require(
            isinstance(e["context"], dict) and type(e["sequence"]) is int and e["sequence"] >= 0,
            "Invalid initial delivery context",
        )
        journal = {
            "schema": "scaffold.delivery/v1",
            "revision": 0,
            "history": [],
            "contract": {
                "objective": feature["objective"],
                "acceptance_criteria": copy.deepcopy(feature["acceptance_criteria"]),
                "constraints": copy.deepcopy(feature.get("constraints", [])),
            },
            "context": e["context"],
            "head": e["head"],
            "sequence": e["sequence"],
            "inflight": None,
        }
        task["delivery"] = journal
    else:
        require(e["kind"] != "initialized", "Delivery history already exists; never reset it")
        if e["kind"] == "context":
            require(
                set(e) == {"kind", "updates", "context"} and isinstance(e["updates"], list),
                "Context requires structured updates",
            )
            require(not journal["inflight"], "Recover active invocation before revising context")
            for update in e["updates"]:
                require(
                    isinstance(update, dict)
                    and update.get("kind")
                    in {
                        "add_obligation",
                        "annotate",
                        "correct_assumption",
                        "revise_design",
                        "repair_check",
                        "add_check",
                        "propose_addressed",
                    "apply_amendment",
                        "diagnose",
                    },
                    "Scope/obligation removal requires explicit approved design transition",
                )
                require(
                    isinstance(update.get("reason"), str) and update["reason"].strip(),
                    "Context update requires reason",
                )
            old, new = journal["context"], e["context"]
            require(
                isinstance(new, dict) and old["original"] == new.get("original"),
                "Original contract cannot be replaced",
            )
            require(
                new.get("updates", [])[: len(old.get("updates", []))] == old.get("updates", []),
                "Context history cannot be removed",
            )
            require(
                all(
                    any(
                        n.get("id") == o["id"] and n.get("text") == o["text"]
                        for n in new.get("obligations", [])
                    )
                    for o in old.get("obligations", [])
                ),
                "Obligations cannot disappear",
            )
            journal["context"] = copy.deepcopy(new)
        elif e["kind"] == "begun":
            require(set(e) == {"kind", "reference", "previous", "sequence"}, "Invalid begin")
            require(not journal["inflight"], "Invocation already reserved; recover, do not replay")
            require(
                e["previous"] == journal["head"] and e["sequence"] == journal["sequence"] + 1,
                "Stale execution head",
            )
            journal.update(inflight=e["reference"], sequence=e["sequence"])
        elif e["kind"] == "finished":
            require(set(e) == {"kind", "reference", "started"}, "Invalid finish")
            require(journal["inflight"] == e["started"], "Missing matching invocation reservation")
            journal.update(head=e["reference"], inflight=None)
        elif e["kind"] == "handoff":
            require(set(e) == {"kind", "previous", "reference"}, "Invalid handoff")
            require(
                not journal["inflight"] and e["previous"] == journal["head"],
                "Handoff must preserve the latest execution history",
            )
            journal["head"] = e["reference"]
        elif e["kind"] == "paused":
            require(
                set(e) == {"kind", "reason"} and isinstance(e["reason"], str),
                "Pause reason required",
            )
        else:
            raise ValueError("Unknown delivery event")
    journal["revision"] += 1
    journal["history"].append({**e, "recorded_at": datetime.now(UTC).isoformat()})
