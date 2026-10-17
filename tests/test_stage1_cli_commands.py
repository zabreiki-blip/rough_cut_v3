"""Dispatch, exit-code and argparse tests for ``stage1.cli``.

Every heavy collaborator (analysis, TTS, validation) is replaced with a mock;
the tests pin how CLI arguments are forwarded and what exit code comes back.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from stage1 import cli


def _json_out(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_analyze_forwards_paths_and_no_llm_flag(capsys) -> None:
    with patch.object(cli, "analyze_script", return_value={"blocks_count": 3}) as analyze:
        code = cli.main(
            ["analyze", "--in", "s.docx", "--out", "o.json", "--raw-out", "r.txt", "--no-llm"]
        )

    assert code == 0
    assert _json_out(capsys) == {"blocks_count": 3}
    kwargs = analyze.call_args.kwargs
    assert kwargs["input_path"] == Path("s.docx")
    assert kwargs["output_path"] == Path("o.json")
    assert kwargs["raw_output_path"] == Path("r.txt")
    assert kwargs["use_llm"] is False


def test_analyze_uses_llm_and_default_output_paths_by_default(capsys) -> None:
    with patch.object(cli, "analyze_script", return_value={}) as analyze:
        assert cli.main(["analyze", "--in", "s.txt"]) == 0

    kwargs = analyze.call_args.kwargs
    assert kwargs["use_llm"] is True
    assert kwargs["output_path"] == Path("work/scenario_prepared.json")
    assert kwargs["raw_output_path"] == Path("work/scenario_raw.txt")


def test_analyze_requires_input_argument(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["analyze"])

    assert exc.value.code == 2
    assert "--in" in capsys.readouterr().err


def test_analyze_error_propagates_instead_of_exit_code() -> None:
    with patch.object(cli, "analyze_script", side_effect=FileNotFoundError("missing")):
        with pytest.raises(FileNotFoundError):
            cli.main(["analyze", "--in", "missing.docx"])


def test_missing_command_and_unknown_command_exit_with_usage_error(capsys) -> None:
    for argv in ([], ["bogus"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 2
    assert "usage" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    "argv",
    [
        ["synthesize", "--in", "p.json", "--provider", "bogus"],
        ["synthesize", "--in", "p.json", "--gender", "other"],
        ["synthesize", "--in", "p.json", "--speed", "fast"],
        ["phrase", "--text", "x", "--provider", "bogus"],
        ["run", "--gender", "other"],
    ],
)
def test_invalid_option_values_are_rejected_by_argparse(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)

    assert exc.value.code == 2


def test_synthesize_forwards_options_and_prints_result(capsys, tmp_path: Path) -> None:
    prepared = tmp_path / "prepared.json"
    speaker = tmp_path / "speaker"
    with (
        patch.object(cli, "synthesize_tts", return_value={"files_count": 2}) as synth,
        patch.object(cli, "_save_cli_revision_assets") as save,
    ):
        code = cli.main(
            [
                "synthesize",
                "--in",
                str(prepared),
                "--out",
                str(speaker),
                "--provider",
                "macos",
                "--speed",
                "1.25",
                "--voice",
                "Milena",
                "--gender",
                "male",
                "--dry-run",
            ]
        )

    assert code == 0
    assert _json_out(capsys) == {"files_count": 2}
    kwargs = synth.call_args.kwargs
    assert kwargs["prepared_path"] == prepared
    assert kwargs["speaker_dir"] == speaker
    assert kwargs["provider"] == "macos"
    assert kwargs["voice_name"] == "Milena"
    assert kwargs["gender"] == "male"
    assert kwargs["speed"] == 1.25
    assert kwargs["dry_run"] is True
    assert kwargs["progress_callback"] is cli._print_progress
    save.assert_called_once_with(prepared, speaker, {"files_count": 2})


def test_synthesize_defaults_do_not_force_provider_or_dry_run() -> None:
    with (
        patch.object(cli, "synthesize_tts", return_value={}) as synth,
        patch.object(cli, "_save_cli_revision_assets"),
    ):
        assert cli.main(["synthesize", "--in", "p.json"]) == 0

    kwargs = synth.call_args.kwargs
    assert kwargs["provider"] is None
    assert kwargs["dry_run"] is False
    assert kwargs["speed"] == 1.0
    assert kwargs["speaker_dir"] == Path("speaker")


def test_normalize_forwards_hint_and_no_llm(capsys) -> None:
    with patch.object(cli, "generate_normalization", return_value={"revision": 1}) as gen:
        code = cli.main(["normalize", "--in", "s.txt", "--hint", "ЗК", "--no-llm"])

    assert code == 0
    assert _json_out(capsys) == {"revision": 1}
    assert gen.call_args.args == (Path("s.txt"), Path("work/scenario_normalized.json"))
    assert gen.call_args.kwargs == {"use_llm": False, "hint": "ЗК"}


def test_confirm_forwards_expected_revision(capsys) -> None:
    with patch.object(cli, "confirm_normalization", return_value={"confirmed": True}) as confirm:
        code = cli.main(["confirm", "--in", "n.json", "--prepared", "p.json", "--revision", "4"])

    assert code == 0
    assert _json_out(capsys) == {"confirmed": True}
    assert confirm.call_args.args == (Path("n.json"), Path("p.json"), Path("work/scenario_raw.txt"))
    assert confirm.call_args.kwargs == {"expected_revision": 4}


def test_confirm_revision_must_be_integer() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["confirm", "--revision", "latest"])

    assert exc.value.code == 2


@pytest.mark.parametrize(("ok", "expected"), [(True, 0), (False, 1), (None, 1)])
def test_validate_exit_code_reflects_validation_result(ok, expected: int, capsys) -> None:
    result = {} if ok is None else {"ok": ok, "errors": [] if ok else ["Missing audio file"]}
    with patch.object(cli, "validate_stage1", return_value=result) as validate:
        code = cli.main(["validate", "--in", "p.json", "--speaker", "sp", "--manifest", "m.json"])

    assert code == expected
    assert _json_out(capsys) == result
    assert validate.call_args.kwargs == {
        "prepared_path": Path("p.json"),
        "speaker_dir": Path("sp"),
        "manifest_path": Path("m.json"),
    }


def test_phrase_forwards_options(capsys) -> None:
    with patch.object(cli, "synthesize_phrase", return_value={"file": "x.wav"}) as phrase:
        code = cli.main(
            [
                "phrase",
                "--text",
                "Привет",
                "--out",
                "sp",
                "--file",
                "hello",
                "--provider",
                "openrouter",
                "--voice",
                "Aoede",
                "--gender",
                "female",
                "--dry-run",
            ]
        )

    assert code == 0
    assert _json_out(capsys) == {"file": "x.wav"}
    assert phrase.call_args.kwargs == {
        "text": "Привет",
        "speaker_dir": Path("sp"),
        "provider": "openrouter",
        "voice_name": "Aoede",
        "gender": "female",
        "file_name": "hello",
        "dry_run": True,
    }


def test_phrase_requires_text() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["phrase"])

    assert exc.value.code == 2


def test_run_returns_one_when_validation_fails(capsys) -> None:
    with (
        patch.object(cli, "is_normalization_confirmed", return_value=True),
        patch.object(cli, "synthesize_tts", return_value={"files_count": 1}),
        patch.object(cli, "_save_cli_revision_assets"),
        patch.object(cli, "validate_stage1", return_value={"ok": False, "errors": ["bad"]}),
    ):
        code = cli.main(["run"])

    out = _json_out(capsys)
    assert code == 1
    assert out["validation"]["errors"] == ["bad"]
    assert out["synthesis"] == {"files_count": 1}


def test_run_does_not_synthesize_when_not_confirmed() -> None:
    with (
        patch.object(cli, "is_normalization_confirmed", return_value=False),
        patch.object(cli, "synthesize_tts") as synth,
        patch.object(cli, "validate_stage1") as validate,
    ):
        assert cli.main(["run", "--normalized", "n.json"]) == 2

    synth.assert_not_called()
    validate.assert_not_called()


def test_print_progress_writes_to_stderr_only(capsys) -> None:
    cli._print_progress({"total": "4", "completed": 2, "id": " zk2 ", "event": "block_done"})
    cli._print_progress({})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["[2/4] block_done zk2", "[0/0] progress"]


def _standard_layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "demo"
    work = root / "work"
    work.mkdir(parents=True)
    prepared = work / "scenario_prepared.json"
    return root, prepared, root / "speaker"


def test_revision_assets_saved_for_standard_layout_with_normalized_file(tmp_path: Path) -> None:
    root, prepared, speaker = _standard_layout(tmp_path)
    (root / "work" / "scenario_normalized.json").write_text("{}", encoding="utf-8")
    result = {"voiceover_assets": []}

    with patch.object(cli, "save_tts_manifest") as save:
        cli._save_cli_revision_assets(prepared, speaker, result)

    save.assert_called_once()
    paths, saved_result = save.call_args.args
    assert paths["root"] == root.resolve()
    assert saved_result is result


def test_revision_assets_skipped_without_normalized_file(tmp_path: Path) -> None:
    _root, prepared, speaker = _standard_layout(tmp_path)

    with patch.object(cli, "save_tts_manifest") as save:
        cli._save_cli_revision_assets(prepared, speaker, {})

    save.assert_not_called()


def test_revision_assets_skipped_for_non_standard_layouts(tmp_path: Path) -> None:
    root, prepared, _speaker = _standard_layout(tmp_path)
    (root / "work" / "scenario_normalized.json").write_text("{}", encoding="utf-8")

    with patch.object(cli, "save_tts_manifest") as save:
        # Prepared file is not inside a ``work`` directory.
        cli._save_cli_revision_assets(tmp_path / "prepared.json", root / "speaker", {})
        # Speaker dir is not the project's ``speaker`` directory.
        cli._save_cli_revision_assets(prepared, tmp_path / "elsewhere", {})

    save.assert_not_called()
