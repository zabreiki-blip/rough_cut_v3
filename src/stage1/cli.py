from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from logging_config import configure_logging

from .analyzer import analyze_script
from .gemini_tts import synthesize_phrase, synthesize_tts
from .normalizer import (
    confirm_normalization,
    generate_normalization,
    is_normalization_confirmed,
)
from .validator import validate_stage1


def _print_progress(event: dict) -> None:
    total = int(event.get("total") or 0)
    completed = int(event.get("completed") or 0)
    block_id = str(event.get("id") or "").strip()
    label = str(event.get("event") or "progress")
    suffix = f" {block_id}" if block_id else ""
    print(f"[{completed}/{total}] {label}{suffix}", file=sys.stderr, flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stage1")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Analyze doc/docx and prepare scenario JSON")
    p_analyze.add_argument("--in", dest="input_path", required=True, help="Input .doc/.docx file")
    p_analyze.add_argument(
        "--out",
        dest="output_path",
        default="work/scenario_prepared.json",
        help="Output prepared JSON path",
    )
    p_analyze.add_argument(
        "--raw-out",
        dest="raw_output_path",
        default="work/scenario_raw.txt",
        help="Raw text output path",
    )
    p_analyze.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable OpenRouter and use fallback splitter only",
    )

    p_tts = sub.add_parser("synthesize", help="Generate TTS files from prepared JSON")
    p_tts.add_argument("--in", dest="prepared_path", required=True, help="Prepared JSON path")
    p_tts.add_argument("--out", dest="speaker_dir", default="speaker", help="Speaker output dir")
    p_tts.add_argument(
        "--provider",
        dest="provider",
        default=None,
        choices=["gemini", "openrouter", "macos"],
        help="TTS provider override",
    )
    p_tts.add_argument("--speed", type=float, default=1.0, help="Playback speed, 0.5-1.5")
    p_tts.add_argument("--voice", dest="voice_name", default=None, help="Explicit voice name")
    p_tts.add_argument(
        "--gender",
        dest="gender",
        default=None,
        choices=["female", "male"],
        help="Voice gender preset",
    )
    p_tts.add_argument(
        "--dry-run",
        action="store_true",
        help="Write silence WAV placeholders instead of real TTS",
    )

    p_validate = sub.add_parser("validate", help="Validate files and build manifest")
    p_validate.add_argument("--in", dest="prepared_path", required=True, help="Prepared JSON path")
    p_validate.add_argument("--speaker", dest="speaker_dir", default="speaker", help="Speaker dir")
    p_validate.add_argument(
        "--manifest",
        dest="manifest_path",
        default="speaker/manifest.json",
        help="Manifest output path",
    )

    p_phrase = sub.add_parser("phrase", help="Generate TTS for one custom phrase")
    p_phrase.add_argument("--text", dest="text", required=True, help="Text to synthesize")
    p_phrase.add_argument("--out", dest="speaker_dir", default="speaker", help="Speaker output dir")
    p_phrase.add_argument(
        "--file",
        dest="file_name",
        default="phrase_preview",
        help="Output filename stem without extension",
    )
    p_phrase.add_argument(
        "--provider",
        dest="provider",
        default=None,
        choices=["gemini", "openrouter", "macos"],
        help="TTS provider override",
    )
    p_phrase.add_argument("--voice", dest="voice_name", default=None, help="Explicit voice name")
    p_phrase.add_argument(
        "--gender",
        dest="gender",
        default=None,
        choices=["female", "male"],
        help="Voice gender preset",
    )
    p_phrase.add_argument(
        "--dry-run",
        action="store_true",
        help="Write silence WAV placeholder instead of real TTS",
    )

    p_normalize = sub.add_parser("normalize", help="Create a canonical normalization draft")
    p_normalize.add_argument("--in", dest="input_path", required=True, help="Input scenario")
    p_normalize.add_argument(
        "--out",
        dest="normalized_path",
        default="work/scenario_normalized.json",
        help="Normalization draft path",
    )
    p_normalize.add_argument("--hint", default="", help="Author marker hint")
    p_normalize.add_argument("--no-llm", action="store_true", help="Disable LLM classification")

    p_confirm = sub.add_parser("confirm", help="Confirm normalization and publish projections")
    p_confirm.add_argument(
        "--in",
        dest="normalized_path",
        default="work/scenario_normalized.json",
        help="Normalization draft path",
    )
    p_confirm.add_argument(
        "--prepared",
        default="work/scenario_prepared.json",
        help="Prepared JSON output",
    )
    p_confirm.add_argument("--raw", default="work/scenario_raw.txt", help="Raw projection output")
    p_confirm.add_argument("--revision", type=int, default=None, help="Expected draft revision")

    p_run = sub.add_parser(
        "run",
        help="Synthesize and validate an already confirmed normalization",
    )
    p_run.add_argument("--normalized", default="work/scenario_normalized.json")
    p_run.add_argument("--prepared", default="work/scenario_prepared.json")
    p_run.add_argument("--speaker", default="speaker")
    p_run.add_argument("--manifest", default="speaker/manifest.json")
    p_run.add_argument("--provider", choices=["gemini", "openrouter", "macos"], default=None)
    p_run.add_argument("--voice", dest="voice_name", default=None)
    p_run.add_argument("--gender", choices=["female", "male"], default=None)
    p_run.add_argument("--speed", type=float, default=1.0)
    p_run.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "analyze":
        result = analyze_script(
            input_path=Path(args.input_path),
            output_path=Path(args.output_path),
            raw_output_path=Path(args.raw_output_path),
            use_llm=not args.no_llm,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "synthesize":
        result = synthesize_tts(
            prepared_path=Path(args.prepared_path),
            speaker_dir=Path(args.speaker_dir),
            provider=args.provider,
            voice_name=args.voice_name,
            gender=args.gender,
            dry_run=args.dry_run,
            speed=args.speed,
            progress_callback=_print_progress,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "normalize":
        result = generate_normalization(
            Path(args.input_path),
            Path(args.normalized_path),
            use_llm=not args.no_llm,
            hint=args.hint,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "confirm":
        result = confirm_normalization(
            Path(args.normalized_path),
            Path(args.prepared),
            Path(args.raw),
            expected_revision=args.revision,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "run":
        normalized_path = Path(args.normalized)
        if not is_normalization_confirmed(normalized_path):
            print(
                "Normalization is not confirmed. Run 'stage1 normalize', review it, then 'stage1 confirm'.",
                file=sys.stderr,
            )
            return 2
        synthesis = synthesize_tts(
            prepared_path=Path(args.prepared),
            speaker_dir=Path(args.speaker),
            provider=args.provider,
            voice_name=args.voice_name,
            gender=args.gender,
            dry_run=args.dry_run,
            speed=args.speed,
            progress_callback=_print_progress,
        )
        validation = validate_stage1(
            prepared_path=Path(args.prepared),
            speaker_dir=Path(args.speaker),
            manifest_path=Path(args.manifest),
        )
        result = {"synthesis": synthesis, "validation": validation}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if validation.get("ok") else 1

    if args.command == "validate":
        result = validate_stage1(
            prepared_path=Path(args.prepared_path),
            speaker_dir=Path(args.speaker_dir),
            manifest_path=Path(args.manifest_path),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.command == "phrase":
        result = synthesize_phrase(
            text=args.text,
            speaker_dir=Path(args.speaker_dir),
            provider=args.provider,
            voice_name=args.voice_name,
            gender=args.gender,
            file_name=args.file_name,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
