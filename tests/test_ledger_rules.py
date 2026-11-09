"""State-machine rules enforced by the ledger: direction, attempts, history and artifacts."""

import copy

import pytest
from flow import (
    approve_direction,
    record_attempt,
    record_checkpoint,
    reference_for,
    sha,
)
from test_tasks import FEATURE, approve_all, evidence, task

from ticket_pipeline.lib import ledger, state_store
from ticket_pipeline.lib.task_store import operate

BASE = FEATURE["baseline"]


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A designed feature in the implement phase, direction NOT yet approved."""
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    operate("init", "F1", FEATURE)
    operate("decompose", "F1", [task("a"), task("b", ["a"])], "F1")
    approve_all()
    return ledger.open_current("F1").path


def feature():
    return operate("list", "F1")["feature"]


def commits(count, label="c"):
    return [
        {"commit": sha(f"{label}{i}"), "parents": [sha(f"{label}{i - 1}")], "subject": f"s{i}"}
        for i in range(count)
    ]


def approval(label="user approval"):
    return operate("artifact-put", "F1", {"approved": label})["reference"]


def foreign(frm=None, count=1, label="u", ref=None):
    items = commits(count, label)
    return {
        "from": frm or feature()["head"],
        "to": items[-1]["commit"],
        "source": "user-commits",
        "commits": items,
        "approval": ref or approval(label),
    }


def snapshot(path):
    return path.read_bytes()


# --- direction approval ----------------------------------------------------------


def test_start_requires_a_matching_direction_approval(fresh):
    before = snapshot(fresh)
    with pytest.raises(ValueError, match="Approve the feature direction"):
        operate("start", "F1", task_id="a")
    assert snapshot(fresh) == before
    scope = feature()["direction_scope"]
    with pytest.raises(ValueError, match="does not match"):
        operate("direction-approve", "F1", {"scope_digest": "0" * 64})
    assert snapshot(fresh) == before
    approve_direction()
    assert feature()["direction"] == {"scope_digest": scope, "capabilities": ["test"]}
    operate("start", "F1", task_id="a")


def test_direction_approval_is_idempotent_and_cannot_be_silently_changed(fresh):
    approve_direction()
    before = snapshot(fresh)
    approve_direction()
    assert snapshot(fresh) == before
    scope = feature()["direction_scope"]
    with pytest.raises(ValueError, match="different capabilities"):
        operate("direction-approve", "F1", {"scope_digest": scope, "capabilities": ["more"]})
    assert snapshot(fresh) == before


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"capabilities": []}, {"scope_digest": "x", "extra": 1}, {"scope_digest": 3}],
)
def test_malformed_direction_approvals_are_rejected(fresh, payload):
    before = snapshot(fresh)
    with pytest.raises(ValueError):
        operate("direction-approve", "F1", payload)
    assert snapshot(fresh) == before


def test_capabilities_must_be_nonempty_strings(fresh):
    scope = feature()["direction_scope"]
    for bad in ("x", [""], [1], [["a"]]):
        with pytest.raises(ValueError, match="capabilities"):
            operate("direction-approve", "F1", {"scope_digest": scope, "capabilities": bad})


def test_a_recreated_feature_instance_needs_its_own_approval(fresh):
    approve_direction()
    old_scope = feature()["direction_scope"]
    from ticket_pipeline.lib.task_reset import apply_reset, plan_reset

    apply_reset(plan_reset(feature_id="F1"))
    operate("init", "F1", FEATURE)
    assert feature()["direction_scope"] != old_scope  # the instance is part of the scope
    assert "direction" not in feature()
    with pytest.raises(ValueError, match="does not match"):
        operate("direction-approve", "F1", {"scope_digest": old_scope})


# --- attempts and completion -----------------------------------------------------


def started(fresh_path, task_id="a"):
    approve_direction()
    operate("start", "F1", task_id=task_id)


def test_completion_must_cite_a_recorded_passed_attempt(fresh):
    started(fresh)
    with pytest.raises(ValueError, match="recorded PASSED attempt"):
        operate("complete", "F1", evidence("a"), "a")
    reference = record_attempt("F1", "a", "NEEDS_DESIGN")
    assert reference == reference_for("a")
    with pytest.raises(ValueError, match="recorded PASSED attempt"):
        operate("complete", "F1", evidence("a"), "a")
    other = operate("artifact-put", "F1", {"receipt": "unrelated"})["reference"]
    with pytest.raises(ValueError, match="recorded PASSED attempt"):
        operate("complete", "F1", {**evidence("a"), "reference": other}, "a")
    passing = record_attempt("F1", "a", "PASSED", number=2)
    with pytest.raises(ValueError, match="recorded PASSED attempt"):
        operate("complete", "F1", evidence("a"), "a")  # cites the failed first attempt's receipt
    done = operate("complete", "F1", {**evidence("a"), "reference": passing}, "a")
    assert done["task"]["status"] == "completed"


def test_a_receipt_from_another_task_cannot_complete_this_one(fresh):
    approve_direction()
    operate("start", "F1", task_id="a")
    record_attempt("F1", "a")
    operate("complete", "F1", evidence("a"), "a")
    record_checkpoint("F1", "a")
    operate("start", "F1", task_id="b")
    with pytest.raises(ValueError, match="recorded PASSED attempt"):
        operate("complete", "F1", {**evidence("b"), "reference": reference_for("a")}, "b")


def test_attempts_are_sequential_idempotent_and_only_for_the_active_task(fresh):
    approve_direction()
    payload = {
        "attempt": 1,
        "status": "BLOCKED",
        "receipt": operate("artifact-put", "F1", {"n": 1})["reference"],
        "base": BASE,
        "tree": sha("t"),
    }
    with pytest.raises(ValueError, match="active issue"):
        operate("attempt-record", "F1", payload, "a")
    operate("start", "F1", task_id="a")
    operate("attempt-record", "F1", payload, "a")
    before = snapshot(fresh)
    operate("attempt-record", "F1", payload, "a")  # replay after a lost response
    assert snapshot(fresh) == before
    with pytest.raises(ValueError, match="differently"):
        operate("attempt-record", "F1", {**payload, "status": "PASSED"}, "a")
    skipped = {
        **payload,
        "attempt": 3,
        "receipt": operate("artifact-put", "F1", {"n": 3})["reference"],
    }
    with pytest.raises(ValueError, match="in sequence"):
        operate("attempt-record", "F1", skipped, "a")
    reused = {**payload, "attempt": 2}
    with pytest.raises(ValueError, match="already used"):
        operate("attempt-record", "F1", reused, "a")
    assert snapshot(fresh) == before


@pytest.mark.parametrize(
    "change",
    [
        {"attempt": 0},
        {"attempt": "1"},
        {"attempt": True},
        {"status": "OK"},
        {"receipt": "receipt-1"},
        {"base": "short"},
        {"tree": None},
        {"extra": 1},
    ],
)
def test_malformed_attempts_are_rejected(fresh, change):
    started(fresh)
    payload = {
        "attempt": 1,
        "status": "PASSED",
        "receipt": operate("artifact-put", "F1", {"n": 1})["reference"],
        "base": BASE,
        "tree": sha("t"),
        **change,
    }
    before = snapshot(fresh)
    with pytest.raises(ValueError):
        operate("attempt-record", "F1", payload, "a")
    assert snapshot(fresh) == before


def test_attempts_may_not_cite_a_missing_artifact(fresh):
    started(fresh)
    before = snapshot(fresh)
    payload = {
        "attempt": 1,
        "status": "PASSED",
        "receipt": "sha256:" + "f" * 64,
        "base": BASE,
        "tree": sha("t"),
    }
    with pytest.raises(ledger.LedgerError, match="missing"):
        operate("attempt-record", "F1", payload, "a")
    assert snapshot(fresh) == before


# --- artifacts --------------------------------------------------------------------


def test_artifacts_round_trip_exactly_and_are_content_addressed(fresh):
    first = operate("artifact-put", "F1", {"b": 2, "a": [1, {"c": None}]})
    again = operate("artifact-put", "F1", {"b": 2, "a": [1, {"c": None}]})
    assert first == again and first["reference"].startswith("sha256:")
    stored = operate("artifact-get", "F1", {"reference": first["reference"]})["artifact"]
    assert stored == {"a": [1, {"c": None}], "b": 2}
    # The writer's key order survives, so a client that re-serializes it recomputes the same bytes.
    assert list(stored) == ["b", "a"]
    before = snapshot(fresh)
    assert snapshot(fresh) == before  # storing an artifact appends no entry


def test_artifact_access_is_validated(fresh):
    for bad in ({"reference": "nope"}, {}, {"reference": "sha256:" + "0" * 64, "x": 1}, None):
        with pytest.raises(ValueError):
            operate("artifact-get", "F1", bad)
    with pytest.raises(ledger.LedgerError, match="missing"):
        operate("artifact-get", "F1", {"reference": "sha256:" + "0" * 64})
    reference = operate("artifact-put", "F1", {"x": 1})["reference"]
    (fresh.parent / "artifacts" / reference.removeprefix("sha256:")).write_bytes(b"swapped")
    with pytest.raises(ledger.LedgerError, match="digest"):
        operate("artifact-get", "F1", {"reference": reference})
    with pytest.raises(ValueError, match="Unknown feature"):
        operate("artifact-put", "NOPE", {"x": 1})


# --- checkpoints and history -------------------------------------------------------


def complete_first(fresh):
    started(fresh)
    record_attempt("F1", "a")
    operate("complete", "F1", evidence("a"), "a")
    return record_checkpoint("F1", "a")


def test_checkpoints_form_one_chain_from_the_original_baseline(fresh):
    assert feature()["head"] == BASE and feature()["history"] == []
    complete_first(fresh)
    first = feature()["history"][0]
    assert first["kind"] == "checkpoint" and first["parent"] == BASE and first["task_id"] == "a"
    assert feature()["head"] == first["commit"]
    operate("start", "F1", task_id="b")
    record_attempt("F1", "b")
    operate("complete", "F1", evidence("b"), "b")
    bad = {
        "commit": sha("x"),
        "parent": BASE,  # not the recorded head
        "tree": sha("t"),
        "reference": reference_for("b"),
    }
    before = snapshot(fresh)
    with pytest.raises(ValueError, match="recorded history head"):
        operate("checkpoint-record", "F1", bad, "b")
    for broken in ({"parent": "zz"}, {"tree": "zz"}, {"commit": first["commit"]}, {"parent": None}):
        with pytest.raises(ValueError):
            operate("checkpoint-record", "F1", {**bad, "parent": first["commit"], **broken}, "b")
    assert snapshot(fresh) == before
    record_checkpoint("F1", "b")
    assert [s["task_id"] for s in feature()["history"]] == ["a", "b"]


def test_foreign_range_moves_the_head_and_checkpoints_continue_from_it(fresh):
    complete_first(fresh)
    checkpoint = feature()["head"]
    payload = foreign(count=2)
    entry_before = len(ledger.open_current("F1").entries)
    operate("history-foreign", "F1", payload)
    assert feature()["head"] == payload["to"]
    assert feature()["history"][-1] == {"kind": "foreign", **payload}
    assert len(ledger.open_current("F1").entries) == entry_before + 1
    operate("start", "F1", task_id="b")
    record_attempt("F1", "b")
    operate("complete", "F1", evidence("b"), "b")
    stale = {
        "commit": sha("n"),
        "parent": checkpoint,
        "tree": sha("t"),
        "reference": reference_for("b"),
    }
    with pytest.raises(ValueError, match="recorded history head"):
        operate("checkpoint-record", "F1", stale, "b")
    record_checkpoint("F1", "b")
    assert feature()["history"][-1]["parent"] == payload["to"]


def test_foreign_history_is_idempotent_and_never_overlaps(fresh):
    payload = foreign()
    operate("history-foreign", "F1", payload)
    before = snapshot(fresh)
    operate("history-foreign", "F1", payload)  # lost-response retry
    assert snapshot(fresh) == before
    with pytest.raises(ValueError, match="start at the recorded head"):
        operate("history-foreign", "F1", foreign(frm=BASE, label="v"))
    extra = commits(1, "z")
    again = {
        **foreign(label="w"),
        "commits": [*payload["commits"], *extra],
        "to": extra[0]["commit"],
        "from": payload["to"],
    }
    with pytest.raises(ValueError, match="already in the recorded history"):
        operate("history-foreign", "F1", again)
    assert snapshot(fresh) == before


def test_history_is_recorded_only_between_tasks(fresh):
    started(fresh)
    before = snapshot(fresh)
    with pytest.raises(ValueError, match="between tasks"):
        operate("history-foreign", "F1", foreign())
    with pytest.raises(ValueError, match="between tasks"):
        operate("history-rewrite", "F1", rewrite_payload(feature()))
    assert snapshot(fresh) == before


@pytest.mark.parametrize(
    "change",
    [
        {"source": "squash"},
        {"from": "abc"},
        {"to": sha("elsewhere")},  # must be the last commit
        {"commits": []},
        {"commits": "x"},
        {"commits": [{"commit": "x"}]},
        {"approval": "ok"},
        {"approval": "sha256:" + "9" * 64},  # not stored
        {"extra": True},
    ],
)
def test_malformed_foreign_history_is_rejected(fresh, change):
    before = snapshot(fresh)
    with pytest.raises((ValueError, ledger.LedgerError)):
        operate("history-foreign", "F1", {**foreign(), **change})
    assert snapshot(fresh) == before


def test_foreign_history_is_bounded_and_noop_ranges_are_rejected(fresh):
    with pytest.raises(ValueError, match="1..200"):
        operate("history-foreign", "F1", foreign(count=201))
    base_commit = {"commit": BASE, "parents": [sha("p")], "subject": "s"}
    with pytest.raises(ValueError, match="Nothing to record"):
        operate("history-foreign", "F1", {**foreign(), "commits": [base_commit], "to": BASE})


def rewrite_payload(current, shift="r", new_base=None):
    # A rebase puts the history on a new base; the default keeps the current one.
    new_base = new_base or current["base"]
    new, previous = [], new_base
    for segment in current["history"]:
        if segment["kind"] == "checkpoint":
            moved = {
                **segment,
                "commit": sha(shift + segment["commit"]),
                "parent": previous,
                "tree": sha(shift + segment["tree"]),
            }
            previous = moved["commit"]
        else:
            items = [{**c, "commit": sha(shift + c["commit"])} for c in segment["commits"]]
            moved = {**segment, "from": previous, "commits": items, "to": items[-1]["commit"]}
            previous = moved["to"]
        new.append(moved)
    return {
        "old_head": current["head"],
        "new_head": previous,
        "new_base": new_base,
        "history": new,
        "verification": operate("artifact-put", "F1", {"verified": shift})["reference"],
    }


def build_history(fresh):
    complete_first(fresh)
    operate("history-foreign", "F1", foreign(count=2))
    return feature()


def test_rewrite_replaces_history_and_keeps_the_old_one(fresh):
    current = build_history(fresh)
    payload = rewrite_payload(current)
    operate("history-rewrite", "F1", payload)
    after = feature()
    assert after["head"] == payload["new_head"] and after["history"] == payload["history"]
    assert after["epochs"] == [
        {
            "base": BASE,
            "head": current["head"],
            "history": current["history"],
            "verification": payload["verification"],
        }
    ]
    assert after["tasks"]["a"]["checkpoint"]["commit"] == payload["history"][0]["commit"]
    assert (
        after["tasks"]["a"]["checkpoint"]["reference"]
        == current["tasks"]["a"]["checkpoint"]["reference"]
    )
    before = snapshot(fresh)
    operate("history-rewrite", "F1", payload)  # replay
    assert snapshot(fresh) == before
    operate("start", "F1", task_id="b")


def test_rewrite_rejects_malformed_or_swapped_foreign_segments(fresh):
    current = build_history(fresh)
    foreign_index = next(i for i, s in enumerate(current["history"]) if s["kind"] == "foreign")
    before = snapshot(fresh)

    def attempt(mutate, match):
        payload = copy.deepcopy(rewrite_payload(current))
        mutate(payload["history"][foreign_index])
        with pytest.raises(ValueError, match=match):
            operate("history-rewrite", "F1", payload)
        assert snapshot(fresh) == before

    attempt(lambda seg: seg["commits"][0].pop("parents"), "Invalid foreign commit")
    attempt(lambda seg: seg["commits"][0].pop("subject"), "Invalid foreign commit")
    attempt(lambda seg: seg.update(approval="sha256:" + "f" * 64), "keep its source, approval")
    attempt(lambda seg: seg.update(source="upstream-merge"), "keep its source, approval")


def test_history_change_clears_the_current_validation_but_a_replay_does_not(fresh):
    build_history(fresh)
    operate("start", "F1", task_id="b")
    passing = record_attempt("F1", "b", "PASSED")
    operate("complete", "F1", {**evidence("b"), "reference": passing}, "b")
    record_checkpoint("F1", "b", passing)
    operate("start", "F1", task_id="F1")
    parent_pass = record_attempt("F1", "F1")
    operate("complete", "F1", {**evidence("F1"), "reference": parent_pass}, "F1")
    record_checkpoint("F1", "F1", parent_pass)
    current = feature()
    feature_state = current
    binding = {
        "feature_id": "F1",
        "design_ref": FEATURE["design_ref"],
        "baseline": BASE,
        "head": current["head"],
        "tree": sha("t"),
        "branch": "feature/test",
        "task_revision": feature_state["task_revision"],
        "plan_digest": "d" * 64,
    }
    operate("validation-start", "F1", {"attempt_id": "x", "binding": binding})
    assert feature()["validation"]["current"] == "x"
    payload = rewrite_payload(current)
    operate("history-rewrite", "F1", payload)
    assert feature()["validation"]["current"] is None
    assert feature()["validation"]["status"] == "awaiting_validation"
    before = snapshot(fresh)
    operate("history-rewrite", "F1", payload)  # identical replay stays a no-op
    assert snapshot(fresh) == before


def test_malformed_rewrites_are_rejected_without_change(fresh):
    current = build_history(fresh)
    good = rewrite_payload(current)
    before = snapshot(fresh)

    def broken(mutate):
        value = copy.deepcopy(good)
        mutate(value)
        return value

    cases = [
        broken(lambda v: v.update(old_head=BASE)),
        broken(lambda v: v.update(new_head=current["head"], history=current["history"])),
        broken(lambda v: v["history"].pop()),
        broken(lambda v: v["history"][0].update(kind="foreign")),
        broken(lambda v: v["history"][0].update(task_id="b")),
        broken(lambda v: v["history"][0].update(parent=sha("wrong"))),
        broken(lambda v: v["history"][1]["commits"].pop()),
        broken(lambda v: v["history"][1].update(**{"from": sha("wrong")})),
        broken(lambda v: v.update(new_head=sha("other"))),
        broken(lambda v: v.update(verification="sha256:" + "8" * 64)),
        broken(lambda v: v.update(verification="nope")),
        broken(lambda v: v.update(extra=1)),
    ]
    for case in cases:
        with pytest.raises((ValueError, ledger.LedgerError)):
            operate("history-rewrite", "F1", case)
    assert snapshot(fresh) == before


# --- validation head ----------------------------------------------------------------


def test_validation_requires_the_ledger_to_account_for_every_commit(fresh):
    approve_direction()
    for task_id in ("a", "b"):
        operate("start", "F1", task_id=task_id)
        record_attempt("F1", task_id)
        operate("complete", "F1", evidence(task_id), task_id)
        record_checkpoint("F1", task_id)
    operate("start", "F1", task_id="F1")
    record_attempt("F1", "F1")
    operate("complete", "F1", evidence("F1"), "F1")
    record_checkpoint("F1", "F1")
    current = feature()
    binding = {
        "feature_id": "F1",
        "design_ref": FEATURE["design_ref"],
        "baseline": BASE,
        "head": sha("unrecorded user commit"),
        "tree": sha("t"),
        "branch": "feature/test",
        "task_revision": current["task_revision"],
        "plan_digest": "d" * 64,
    }
    before = snapshot(fresh)
    with pytest.raises(ValueError, match="recorded history head"):
        operate("validation-start", "F1", {"attempt_id": "x", "binding": binding})
    assert snapshot(fresh) == before
    operate(
        "validation-start",
        "F1",
        {"attempt_id": "x", "binding": {**binding, "head": current["head"]}},
    )


# --- the whole lifecycle replays from the ledger alone ------------------------------


def test_full_lifecycle_state_equals_the_fold_of_its_ledger(fresh):
    approve_direction()
    for task_id in ("a", "b"):
        operate("start", "F1", task_id=task_id)
        record_attempt("F1", task_id, "NEEDS_DESIGN")
        passing = record_attempt("F1", task_id, "PASSED", number=2)
        operate("complete", "F1", {**evidence(task_id), "reference": passing}, task_id)
        record_checkpoint("F1", task_id, passing)
        if task_id == "a":
            operate("history-foreign", "F1", foreign(count=3))
    live = ledger.open_current("F1")
    reopened = ledger.Ledger(live.directory)  # independent read, chain verified end to end
    assert reopened.state == live.state
    assert reopened.state["head"] == feature()["head"]
    types = [e["type"] for e in reopened.entries]
    assert types.count("attempt-record") == 4 and "history-foreign" in types
    assert all(e["seq"] == i for i, e in enumerate(reopened.entries))
    # Every artifact an entry cites is present and verifies.
    for entry in reopened.entries:
        for key in ("receipt", "approval", "verification"):
            payload = entry["body"].get("payload")
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and value.startswith("sha256:"):
                reopened.get_artifact(value.removeprefix("sha256:"))


def test_foreign_segments_keep_their_own_kind_marker(fresh):
    operate("history-foreign", "F1", foreign())
    segment = feature()["history"][-1]
    assert segment["kind"] == "foreign" and segment["source"] == "user-commits"


def test_rewrite_onto_a_new_base_moves_the_base_and_keeps_the_original_baseline(fresh):
    current = build_history(fresh)
    assert current["base"] == BASE
    upstream = sha("new upstream tip")
    payload = rewrite_payload(current, new_base=upstream)
    assert payload["history"][0]["parent"] == upstream  # the chain now starts at the new base
    operate("history-rewrite", "F1", payload)
    after = feature()
    assert after["base"] == upstream and after["original_baseline"] == BASE
    assert after["epochs"][-1]["base"] == BASE and after["epochs"][-1]["head"] == current["head"]
    before = snapshot(fresh)
    operate("history-rewrite", "F1", payload)  # replay
    assert snapshot(fresh) == before
    # A second rebase chains from the new base, and every epoch is kept.
    again = rewrite_payload(after, shift="s", new_base=sha("later upstream"))
    operate("history-rewrite", "F1", again)
    assert [e["base"] for e in feature()["epochs"]] == [BASE, upstream]


def test_a_rewrite_that_does_not_continue_from_its_new_base_is_rejected(fresh):
    current = build_history(fresh)
    payload = rewrite_payload(current, new_base=sha("upstream"))
    before = snapshot(fresh)
    for broken in (
        {"new_base": sha("someone else")},  # history no longer starts at the new base
        {"new_base": "short"},
        {"new_base": None},
    ):
        with pytest.raises((ValueError, ledger.LedgerError)):
            operate("history-rewrite", "F1", {**payload, **broken})
    with pytest.raises(ValueError):
        operate("history-rewrite", "F1", {k: v for k, v in payload.items() if k != "new_base"})
    assert snapshot(fresh) == before


def test_a_rewrite_that_changes_neither_head_nor_base_is_rejected(fresh):
    current = build_history(fresh)
    same = {
        "old_head": current["head"],
        "new_head": current["head"],
        "new_base": current["base"],
        "history": current["history"],
        "verification": operate("artifact-put", "F1", {"v": "same"})["reference"],
    }
    with pytest.raises(ValueError, match="Nothing was rewritten"):
        operate("history-rewrite", "F1", same)


def test_validation_binds_to_the_history_base_not_the_original_baseline(fresh):
    approve_direction()
    for task_id in ("a", "b", "F1"):
        operate("start", "F1", task_id=task_id)
        record_attempt("F1", task_id)
        operate("complete", "F1", evidence(task_id), task_id)
        record_checkpoint("F1", task_id)
    current = feature()
    upstream = sha("rebased onto")
    operate("history-rewrite", "F1", rewrite_payload(current, new_base=upstream))
    moved = feature()
    binding = {
        "feature_id": "F1",
        "design_ref": FEATURE["design_ref"],
        "baseline": BASE,
        "head": moved["head"],
        "tree": sha("t"),
        "branch": "feature/test",
        "task_revision": moved["task_revision"],
        "plan_digest": "d" * 64,
    }
    with pytest.raises(ValueError, match="history base"):
        operate("validation-start", "F1", {"attempt_id": "x", "binding": binding})
    operate(
        "validation-start", "F1", {"attempt_id": "x", "binding": {**binding, "baseline": upstream}}
    )
