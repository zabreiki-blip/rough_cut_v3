from __future__ import annotations

from pathlib import Path
import re

import api

README = Path(__file__).resolve().parents[1] / "README.md"
_SECTION_START = "Эндпоинты:"
_ENTRY = re.compile(r"^- `([A-Z|]+) (/[^`\s?]*)(?:\?[^`]*)?`")
_SERVICE_METHODS = {"HEAD", "OPTIONS"}


def _strip_converters(path: str) -> str:
    # Flask writes `<int:rule_id>`, README documents the same as `<rule_id>`.
    return re.sub(r"<(?:[a-z_]+:)?([^>]+)>", r"<\1>", path)


def _url_map_pairs() -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for rule in api.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for method in rule.methods - _SERVICE_METHODS:
            pairs.add((method, _strip_converters(rule.rule)))
    return pairs


def _readme_pairs() -> set[tuple[str, str]]:
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index(_SECTION_START) + 1
    pairs: set[tuple[str, str]] = set()
    for line in lines[start:]:
        if line.strip() and not line.startswith("- "):
            break
        match = _ENTRY.match(line)
        if not match:
            continue
        for method in match.group(1).split("|"):
            pairs.add((method, match.group(2)))
    return pairs


def test_readme_documents_every_route() -> None:
    missing = sorted(_url_map_pairs() - _readme_pairs())
    assert not missing, f"endpoints missing in README API section: {missing}"


def test_readme_lists_only_existing_routes() -> None:
    unknown = sorted(_readme_pairs() - _url_map_pairs())
    assert not unknown, f"README API section lists unknown endpoints: {unknown}"
