from __future__ import annotations

from unittest.mock import patch

from stage1 import cli


def test_run_refuses_unconfirmed_normalization(capsys) -> None:
    with patch.object(cli, "is_normalization_confirmed", return_value=False):
        result = cli.main(["run"])

    assert result == 2
    assert "not confirmed" in capsys.readouterr().err


def test_run_synthesizes_and_validates_confirmed_normalization(capsys) -> None:
    with (
        patch.object(cli, "is_normalization_confirmed", return_value=True),
        patch.object(cli, "synthesize_tts", return_value={"files_count": 2}) as synthesize,
        patch.object(cli, "validate_stage1", return_value={"ok": True}) as validate,
    ):
        result = cli.main(["run", "--dry-run", "--speed", "1.1"])

    assert result == 0
    assert '"validation"' in capsys.readouterr().out
    assert synthesize.call_args.kwargs["speed"] == 1.1
    assert synthesize.call_args.kwargs["dry_run"] is True
    validate.assert_called_once()


def test_cli_exposes_canonical_normalization_commands() -> None:
    parser = cli._build_parser()

    assert parser.parse_args(["normalize", "--in", "scenario.docx"]).command == "normalize"
    assert parser.parse_args(["confirm"]).command == "confirm"
