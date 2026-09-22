import pytest

from ticket_pipeline.lib import stack_store
from ticket_pipeline.lib.stack_store import CriterionFrame


def _set_paths(tmp_path, monkeypatch):
    scaffold_dir = tmp_path / ".scaffold"
    monkeypatch.setattr(stack_store, "SCAFFOLD_DIR", scaffold_dir)
    monkeypatch.setattr(stack_store, "STACK_FILE", scaffold_dir / ".criteria-stack.json")
    monkeypatch.setattr(stack_store, "STACK_LOCK_FILE", scaffold_dir / ".criteria-stack.lock")


def test_push_and_pop_round_trip(tmp_path, monkeypatch):
    _set_paths(tmp_path, monkeypatch)

    stack_store.push_frame(CriterionFrame(ticket="SA-1", criterion="- [ ] First"))
    stack_store.push_frame(CriterionFrame(ticket="SA-1", criterion="- [ ] Second"))

    stack = stack_store.load_stack()
    assert [f.criterion for f in stack] == ["- [ ] Second", "- [ ] First"]

    top = stack_store.pop_frame()
    assert top is not None
    assert top.criterion == "- [ ] Second"
    assert [f.criterion for f in stack_store.load_stack()] == ["- [ ] First"]


def test_clear_stack(tmp_path, monkeypatch):
    _set_paths(tmp_path, monkeypatch)

    stack_store.push_frame(CriterionFrame(ticket="SA-1", criterion="- [ ] First"))
    stack_store.clear_stack()
    assert stack_store.load_stack() == []


def test_save_stack_round_trip(tmp_path, monkeypatch):
    _set_paths(tmp_path, monkeypatch)

    frames = [CriterionFrame(ticket="SA-2", criterion="- [ ] Validate state")]
    stack_store.save_stack(frames)

    loaded = stack_store.load_stack()
    assert len(loaded) == 1
    assert loaded[0].ticket == "SA-2"


def test_load_stack_rejects_non_string_required_fields(tmp_path, monkeypatch):
    _set_paths(tmp_path, monkeypatch)
    stack_store.SCAFFOLD_DIR.mkdir(parents=True, exist_ok=True)
    stack_store.STACK_FILE.write_text('[{"ticket": 123, "criterion": true}]\n', encoding="utf-8")

    with pytest.raises(ValueError):
        stack_store.load_stack()


def test_load_stack_rejects_non_string_optional_fields(tmp_path, monkeypatch):
    _set_paths(tmp_path, monkeypatch)
    stack_store.SCAFFOLD_DIR.mkdir(parents=True, exist_ok=True)
    stack_store.STACK_FILE.write_text(
        '[{"ticket": "SA-1", "criterion": "- [ ] x", "status": 5}]\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        stack_store.load_stack()
