import copy
import json
import multiprocessing
import os
import random
import sys

import pytest

from ticket_pipeline.lib import ledger, state_store

INSTANCE = "a" * 32


@pytest.fixture
def scaffold(tmp_path, monkeypatch):
    monkeypatch.setattr(state_store, "SCAFFOLD_DIR", tmp_path / ".scaffold")
    monkeypatch.setattr(state_store, "STATE_LOCK_FILE", tmp_path / ".scaffold/.tasks.lock")
    return tmp_path


def fresh(feature="F", instance=INSTANCE):
    return ledger.create(feature, instance)


def lines(led):
    return led.path.read_bytes().split(b"\n")[:-1]


def rewrite(led, mutate):
    entries = [json.loads(x) for x in lines(led)]
    mutate(entries)
    led.path.write_bytes(b"".join(ledger.canonical(e) + b"\n" for e in entries))


# --- patches -------------------------------------------------------------------


def random_state(rng, depth=0):
    state = {}
    for key in rng.sample("abcdefg", rng.randint(0, 5)):
        kinds = ["int", "str", "list", "dict", "bool", "none"] if depth < 3 else ["int", "str"]
        kind = rng.choice(kinds)
        if kind == "int":
            state[key] = rng.randint(0, 3)
        elif kind == "str":
            state[key] = rng.choice(["x", "y", ""])
        elif kind == "bool":
            state[key] = rng.choice([True, False])
        elif kind == "none":
            state[key] = None
        elif kind == "list":
            state[key] = [rng.randint(0, 2) for _ in range(rng.randint(0, 4))]
        else:
            state[key] = random_state(rng, depth + 1)
    return state


def test_patch_round_trips_arbitrary_state_changes():
    rng = random.Random(7)
    for _ in range(400):
        old, new = random_state(rng), random_state(rng)
        patch = ledger.diff(old, new)
        assert json.loads(json.dumps(patch)) == patch  # JSON-safe
        assert ledger.apply_patch(old, patch) == new
        assert ledger.digest(ledger.apply_patch(old, patch)) == ledger.digest(new)


def test_patch_appends_to_growing_lists_instead_of_rewriting_them():
    old = {"history": [{"n": 1}, {"n": 2}], "x": 1}
    new = {"history": [{"n": 1}, {"n": 2}, {"n": 3}], "x": 1}
    assert ledger.diff(old, new) == [{"op": "append", "path": ["history"], "values": [{"n": 3}]}]
    assert ledger.diff(new, old) == [{"op": "set", "path": ["history"], "value": old["history"]}]


def test_patch_distinguishes_bool_from_int_and_ignores_no_change():
    assert ledger.diff({"a": 1}, {"a": 1}) == []
    assert ledger.diff({"a": 1}, {"a": True}) == [{"op": "set", "path": ["a"], "value": True}]
    assert ledger.diff({"a": [1]}, {"a": [True]}) == [{"op": "set", "path": ["a"], "value": [True]}]


@pytest.mark.parametrize(
    "ops",
    [
        [{"op": "set", "path": [], "value": 1}],
        [{"op": "set", "path": ["a", "b"], "value": 1}],
        [{"op": "del", "path": ["missing"]}],
        [{"op": "append", "path": ["x"], "values": [1]}],
        [{"op": "mystery", "path": ["x"]}],
        [{"op": "set", "path": [1], "value": 1}],
    ],
)
def test_malformed_patches_are_rejected(ops):
    with pytest.raises(ledger.LedgerError):
        ledger.apply_patch({"x": 1}, ops)


# --- chain ---------------------------------------------------------------------


def test_entries_chain_and_state_folds(scaffold):
    led = fresh()
    first = led.append("init", {"payload": 1}, {"id": "F", "n": 0, "log": []})
    second = led.append("step", {"payload": 2}, {"id": "F", "n": 1, "log": ["a"]})
    assert first["seq"] == 0 and first["prev"] == ledger.GENESIS
    assert second["prev"] == first["hash"] and second["seq"] == 1
    assert led.head == second["hash"]
    reopened = ledger.Ledger(led.directory)
    assert reopened.state == {"id": "F", "n": 1, "log": ["a"]}
    assert reopened.find(first["hash"]) == first
    assert reopened.find("0" * 64) is None
    assert [e["type"] for e in reopened.entries] == ["init", "step"]


def test_expected_sequence_rejects_stale_writers(scaffold):
    led = fresh()
    led.append("init", {}, {"id": "F"}, expected_seq=0)
    with pytest.raises(ValueError, match="Stale"):
        led.append("late", {}, {"id": "F", "n": 1}, expected_seq=0)
    assert len(ledger.Ledger(led.directory).entries) == 1


def build(scaffold, count=4):
    led = fresh()
    state = {"id": "F", "log": []}
    led.append("init", {}, state)
    for index in range(count):
        state = {**state, "log": [*state["log"], index]}
        led.append("step", {"i": index}, state)
    return led


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e[2]["body"].update(i=99),  # altered audit body
        lambda e: e[2]["patch"].append({"op": "set", "path": ["evil"], "value": 1}),
        lambda e: e[2].update(at="2000-01-01T00:00:00+00:00"),
        lambda e: e.insert(2, copy.deepcopy(e[1])),  # duplicated entry
        lambda e: e.pop(2),  # removed from the middle
        lambda e: e.reverse(),
        lambda e: e[1].update(prev=ledger.GENESIS),
        lambda e: e[3].update(after="0" * 64),
        lambda e: e[1].update(extra=True),
    ],
)
def test_any_edit_to_history_is_detected(scaffold, mutate):
    led = build(scaffold)
    rewrite(led, mutate)
    with pytest.raises(ledger.LedgerError):
        ledger.Ledger(led.directory)


def test_a_consistent_forgery_still_needs_every_later_hash_recomputed(scaffold):
    led = build(scaffold)

    def forge(entries):
        entries[1]["body"] = {"forged": True}
        # Recomputing only that entry's hash leaves the next entry's `prev` stale.
        entries[1]["hash"] = ledger.digest({k: v for k, v in entries[1].items() if k != "hash"})

    rewrite(led, forge)
    with pytest.raises(ledger.LedgerError, match="follow"):
        ledger.Ledger(led.directory)


def test_garbage_in_the_committed_region_fails_closed(scaffold):
    led = build(scaffold, 2)
    data = led.path.read_bytes()
    led.path.write_bytes(data.replace(b'"seq":1', b'"seq":oops', 1))
    with pytest.raises(ledger.LedgerError):
        ledger.Ledger(led.directory)


def test_torn_final_write_is_ignored_then_overwritten(scaffold):
    led = build(scaffold, 2)
    committed = led.path.read_bytes()
    led.path.write_bytes(committed + b'{"seq":3,"prev":"abc","ty')  # crash mid-write
    reopened = ledger.Ledger(led.directory)
    assert reopened.state == led.state and len(reopened.entries) == 3
    reopened.append("after-crash", {}, {**led.state, "n": 9})
    recovered = ledger.Ledger(led.directory)
    assert len(recovered.entries) == 4 and recovered.state["n"] == 9
    assert recovered.path.read_bytes().startswith(committed)


def test_empty_and_missing_ledgers_are_empty(scaffold):
    led = fresh()
    assert led.entries == [] and led.state == {} and led.head == ledger.GENESIS
    led.directory.mkdir(parents=True)
    led.path.write_bytes(b"")
    assert ledger.Ledger(led.directory).entries == []


# --- artifacts -----------------------------------------------------------------


def test_artifacts_are_content_addressed_and_verified_on_read(scaffold):
    led = fresh()
    name = led.put_artifact(b"review output")
    assert name == ledger.hashlib.sha256(b"review output").hexdigest()
    assert led.put_artifact(b"review output") == name
    assert led.get_artifact(name) == b"review output"
    (led.directory / "artifacts" / name).write_bytes(b"swapped")
    with pytest.raises(ledger.LedgerError):
        led.get_artifact(name)
    with pytest.raises(ledger.LedgerError):
        led.put_artifact(b"review output")
    with pytest.raises(ledger.LedgerError, match="missing"):
        led.get_artifact("f" * 64)
    for bad in ("../x", "", "A" * 64, None):
        with pytest.raises(ValueError):
            led.get_artifact(bad)


# --- layout and safety ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a.b", "a b", None, 7])
def test_feature_ids_cannot_escape_or_collide_with_the_instance_suffix(scaffold, bad):
    with pytest.raises(ValueError):
        ledger.require_feature_id(bad)


def test_instances_are_separate_and_only_one_may_be_open(scaffold):
    first = fresh()
    first.append("init", {}, {"id": "F"})
    assert ledger.open_current("F").directory == first.directory
    assert ledger.open_current("OTHER") is None
    first.append("close", {}, {"id": "F", "closed": {"reason": "done"}})
    assert ledger.open_current("F") is None  # history stays; the instance is simply closed
    second = fresh(instance="b" * 32)
    second.append("init", {}, {"id": "F"})
    assert ledger.open_current("F").directory == second.directory
    assert len(ledger.instance_directories("F")) == 2
    third = fresh(instance="c" * 32)
    third.append("init", {}, {"id": "F"})
    with pytest.raises(ledger.LedgerError, match="More than one"):
        ledger.open_current("F")


def test_an_instance_directory_is_never_reused(scaffold):
    led = fresh()
    led.append("init", {}, {"id": "F"})
    with pytest.raises(ValueError, match="already exists"):
        fresh()


def test_empty_directory_from_a_crash_before_the_first_entry_is_not_an_open_instance(scaffold):
    ledger.instance_dir("F", INSTANCE).mkdir(parents=True)
    assert ledger.open_current("F") is None


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
def test_symbolic_links_in_ledger_paths_are_refused(scaffold, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".scaffold").mkdir()
    (tmp_path / ".scaffold" / "features").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        ledger.open_current("F")
    with pytest.raises(ValueError, match="symbolic link"):
        fresh().append("init", {}, {"id": "F"})


# --- concurrency ---------------------------------------------------------------


def _worker(root, count, tag):
    sys.path.insert(0, str(root))
    from ticket_pipeline.lib import ledger as L
    from ticket_pipeline.lib import state_store as S

    S.SCAFFOLD_DIR = type(S.SCAFFOLD_DIR)(root) / ".scaffold"
    S.STATE_LOCK_FILE = S.SCAFFOLD_DIR / ".tasks.lock"
    for index in range(count):
        with S.state_lock():
            led = L.open_current("F")
            state = {**led.state, "log": [*led.state["log"], f"{tag}{index}"]}
            led.append("step", {"tag": tag}, state)


def test_concurrent_processes_serialize_into_one_valid_chain(scaffold):
    led = fresh()
    led.append("init", {}, {"id": "F", "log": []})
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_worker, args=(str(scaffold), 15, tag)) for tag in "pqr"]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    final = ledger.Ledger(led.directory)
    assert len(final.entries) == 1 + 45
    assert sorted(final.state["log"]) == sorted(f"{t}{i}" for t in "pqr" for i in range(15))
    for tag in "pqr":  # per-writer order preserved
        mine = [x for x in final.state["log"] if x[0] == tag]
        assert mine == [f"{tag}{i}" for i in range(15)]


def test_documented_limit_removing_trailing_entries_is_not_detectable(scaffold):
    # The chain detects edits, reordering and removal from the middle. Dropping the
    # newest whole entries leaves a valid shorter chain; only an external copy of the
    # head hash could notice. This test records that limit so it is never mistaken
    # for a guarantee.
    led = build(scaffold, 3)
    kept = lines(led)[:3]
    led.path.write_bytes(b"".join(x + b"\n" for x in kept))
    shorter = ledger.Ledger(led.directory)
    assert len(shorter.entries) == 3 and shorter.state["log"] == [0, 1]


def test_directory_sync_failure_rolls_the_append_back(scaffold, monkeypatch):
    led = fresh()
    led.append("a", {}, {"n": 1})
    committed = led.path.read_bytes()

    def boom():
        raise OSError("fsync failed")

    monkeypatch.setattr(led, "_fsync_directory", boom)
    with pytest.raises(OSError):
        led.append("a", {}, {"n": 2})
    assert led.path.read_bytes() == committed
    assert len(led.entries) == 1 and led.state == {"n": 1}
    monkeypatch.undo()
    led.append("a", {}, {"n": 2})
    assert len(lines(led)) == 2
