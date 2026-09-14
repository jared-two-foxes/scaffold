from ticket_pipeline import status
from ticket_pipeline.lib.stack_store import CriterionFrame


def test_status_shows_empty_stack(capsys, monkeypatch):
    monkeypatch.setattr(status, "load_stack", lambda: [])
    status.main()
    assert "Stack is empty" in capsys.readouterr().out


def test_status_shows_top_frame(capsys, monkeypatch):
    monkeypatch.setattr(
        status,
        "load_stack",
        lambda: [CriterionFrame(ticket="SA-1", criterion="- [ ] First", status="pending")],
    )
    status.main()
    out = capsys.readouterr().out
    assert "Ticket: SA-1" in out
    assert "Criteria remaining: 1" in out
