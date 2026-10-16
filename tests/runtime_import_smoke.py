"""Check production ASR imports without constructing or downloading a model.

Run after installing requirements.txt and requirements-whisper.txt. This is a
standalone CI smoke check, so it does not require pytest or API credentials.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import faster_whisper  # noqa: E402, F401
import stage2.cloud_asr  # noqa: E402, F401
import stage2.whisper_runner  # noqa: E402, F401


if __name__ == "__main__":
    print("Production ASR imports passed; no model loaded.")
