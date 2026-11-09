"""Git history as the ledger sees it: checkpoints, foreign ranges and rewrites.

``feature["history"]`` is the ordered list of segments from the original baseline to
``feature["head"]``. Every commit Scaffold knows about is in exactly one segment:

* ``checkpoint``: the verified commit of one completed task,
* ``foreign``: a user-approved range of commits that are not task checkpoints (they stay in
  review scope; being recorded does not make them verified).

Scaffold enforces that the segments form one unbroken chain and that nothing is recorded while
work is active. It cannot see Git: Pi confirms every commit, tree and ancestry before appending.
``rewrite`` replaces the segment list after a history rewrite (rebase) and keeps the previous
list in ``epochs``.
"""

from __future__ import annotations

import copy

from ticket_pipeline.lib.refs import is_commit, is_reference, require

ACTIONS = {"history-foreign", "history-rewrite"}
SOURCES = {"user-commits", "upstream-merge"}
MAX_COMMITS = 200


def initialize(feature: dict) -> None:
    feature["head"] = feature["original_baseline"]
    feature["history"] = []
    feature["epochs"] = []


def append_checkpoint(feature: dict, task_id: str, commit: str, parent: str, tree: str) -> None:
    require(parent == feature["head"], "Checkpoint must follow the recorded history head")
    require(commit != parent, "Checkpoint must be a new commit")
    feature["history"].append(
        {"kind": "checkpoint", "task_id": task_id, "commit": commit, "parent": parent, "tree": tree}
    )
    feature["head"] = commit


def _no_active_work(feature: dict) -> None:
    require(
        not any(t["status"] == "in_progress" for t in feature["tasks"].values()),
        "Record history only between tasks",
    )


def _foreign(feature: dict, payload: object) -> None:
    fields = {"from", "to", "source", "commits", "approval"}
    require(
        isinstance(payload, dict) and set(payload) == fields,
        "Foreign history requires " + ", ".join(sorted(fields)),
    )
    require(payload["source"] in SOURCES, "Unknown foreign history source")
    require(is_commit(payload["from"]) and is_commit(payload["to"]), "Full commit SHAs required")
    require(is_reference(payload["approval"]), "A retained user approval is required")
    commits = payload["commits"]
    require(
        isinstance(commits, list) and 0 < len(commits) <= MAX_COMMITS,
        "Foreign history needs 1.." + str(MAX_COMMITS) + " commits",
    )
    for item in commits:
        require(
            isinstance(item, dict)
            and set(item) == {"commit", "parents", "subject"}
            and is_commit(item["commit"])
            and isinstance(item["parents"], list)
            and item["parents"]
            and all(is_commit(p) for p in item["parents"])
            and isinstance(item["subject"], str),
            "Invalid foreign commit entry",
        )
    require(commits[-1]["commit"] == payload["to"], "The range must end at its target commit")
    last = feature["history"][-1] if feature["history"] else None
    if last is not None and last == {"kind": "foreign", **payload}:
        return  # idempotent replay of the last segment
    already = {s["commit"] for s in feature["history"] if s["kind"] == "checkpoint"}
    already |= {
        c["commit"] for s in feature["history"] if s["kind"] == "foreign" for c in s["commits"]
    }
    require(
        not any(c["commit"] in already for c in commits),
        "Commit is already in the recorded history",
    )
    _no_active_work(feature)
    require(payload["from"] == feature["head"], "History range must start at the recorded head")
    require(payload["to"] != payload["from"], "Nothing to record")
    feature["history"].append({"kind": "foreign", **copy.deepcopy(payload)})
    feature["head"] = payload["to"]


def _rewrite(feature: dict, payload: object) -> None:
    fields = {"old_head", "new_head", "history", "verification"}
    require(
        isinstance(payload, dict) and set(payload) == fields,
        "Rewrite requires " + ", ".join(sorted(fields)),
    )
    require(is_reference(payload["verification"]), "A retained verification is required")
    require(
        is_commit(payload["old_head"]) and is_commit(payload["new_head"]),
        "Full commit SHAs required",
    )
    if (
        feature["head"] == payload["new_head"]
        and feature["epochs"]
        and feature["epochs"][-1]["verification"] == payload["verification"]
    ):
        return  # idempotent replay
    _no_active_work(feature)
    require(payload["old_head"] == feature["head"], "Rewrite must start from the recorded head")
    require(payload["new_head"] != payload["old_head"], "Nothing was rewritten")
    new, old = payload["history"], feature["history"]
    require(
        isinstance(new, list) and len(new) == len(old), "Rewrite must keep every recorded segment"
    )
    previous = feature["original_baseline"]
    for before, after in zip(old, new, strict=True):
        require(
            isinstance(after, dict) and after.get("kind") == before["kind"], "Segment kind changed"
        )
        if before["kind"] == "checkpoint":
            require(
                set(after) == set(before)
                and after["task_id"] == before["task_id"]
                and is_commit(after["commit"])
                and is_commit(after["tree"])
                and after["parent"] == previous,
                "Rewritten checkpoint must keep its task and continue the chain",
            )
            previous = after["commit"]
        else:
            require(
                set(after) == set(before)
                and after["from"] == previous
                and len(after["commits"]) == len(before["commits"])
                and after["commits"][-1]["commit"] == after["to"],
                "Rewritten foreign range must keep its size and continue the chain",
            )
            previous = after["to"]
    require(previous == payload["new_head"], "Rewritten history must end at the new head")
    feature["epochs"].append(
        {
            "head": payload["old_head"],
            "history": copy.deepcopy(old),
            "verification": payload["verification"],
        }
    )
    feature["history"] = copy.deepcopy(new)
    feature["head"] = payload["new_head"]
    for segment in new:
        if segment["kind"] == "checkpoint":
            task = feature["tasks"][segment["task_id"]]
            task["checkpoint"] = {
                **task["checkpoint"],
                "commit": segment["commit"],
                "parent": segment["parent"],
                "tree": segment["tree"],
            }


def transition(feature: dict, action: str, payload: object) -> None:
    {"history-foreign": _foreign, "history-rewrite": _rewrite}[action](feature, payload)
