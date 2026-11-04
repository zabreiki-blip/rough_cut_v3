"""Initialize local settings without displaying or overwriting existing secrets."""

import os
from pathlib import Path
import re
import secrets
import sys
import tempfile


def initialize(root: Path, native_macos: bool) -> None:
    target = root / ".env"
    created = not target.exists()
    content = (root / ".env.example" if created else target).read_bytes().decode()
    if created and native_macos:
        content = re.sub(r"(?m)^TTS_PROVIDER=.*$", "TTS_PROVIDER=macos", content)
    token = re.compile(r"(?m)^([ \t]*(?:export[ \t]+)?ROUGH_CUT_API_TOKEN[ \t]*=)([^\r\n]*)")
    matches = list(token.finditer(content))

    # Quoted empty values and whitespace/comment-only values are empty dotenv values.
    def blank(value: str) -> bool:
        value = value.strip()
        return (
            not value
            or value.startswith("#")
            or bool(re.fullmatch(r"""(?:"[ \t]*"|'[ \t]*')[ \t]*(?:#.*)?""", value))
        )

    if not matches:
        content += (
            ("" if not content or content.endswith("\n") else "\n")
            + "ROUGH_CUT_API_TOKEN="
            + secrets.token_hex(32)
            + "\n"
        )
    elif blank(matches[-1].group(2)):
        generated = secrets.token_hex(32)
        content = token.sub(
            lambda m: m.group(1) + generated if blank(m.group(2)) else m.group(0), content
        )
    original = target.read_bytes() if target.exists() else None
    if original != content.encode():
        descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content.encode())
            if not created:
                os.chmod(temporary, target.stat().st_mode & 0o7777)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


if __name__ == "__main__":
    initialize(Path(sys.argv[1]), sys.argv[2] == "macos")
