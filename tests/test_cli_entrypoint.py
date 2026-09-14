import pytest

from scaffold_cli import cli


def test_top_level_help(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "fetch-ticket" in out
    assert "status" in out
    assert "stack" in out
