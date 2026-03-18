from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .analyzer import analyze_script
from .gemini_tts import synthesize_phrase, synthesize_tts
from .validator import validate_stage1


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
    return parser


def main(argv: list[str] | None = None) -> int:
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
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

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
