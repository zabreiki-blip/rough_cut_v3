"""Check the real demo export; this does not replace a Premiere import."""

import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


def check_xml(filename: Path) -> dict:
    root = ET.parse(filename).getroot()
    sequence = root.find(".//sequence[@id='sequence-rough-cut']")
    assert sequence is not None, "Missing rough-cut sequence"
    clips = sorted(
        sequence.findall("./media/audio/track/clipitem"), key=lambda c: int(c.findtext("start"))
    )
    assert [c.findtext("name") for c in clips] == ["zk1", "RC_SRC_review", "zk2"]
    assert int(clips[0].findtext("start")) == 0
    assert all(a.findtext("end") == b.findtext("start") for a, b in zip(clips, clips[1:]))
    assert clips[-1].findtext("end") == sequence.findtext("duration")
    media = [Path(unquote(urlparse(p.text).path)) for p in root.findall(".//pathurl")]
    assert media and all(p.is_file() for p in media), "Export references missing media"
    return {
        "audio_clip_order": [c.findtext("name") for c in clips],
        "sequence_duration_frames": int(sequence.findtext("duration")),
        "timebase": int(sequence.findtext("rate/timebase")),
        "all_linked_files_exist": True,
        "media": [str(p) for p in media],
        "premiere_import": "NOT VERIFIED",
    }


if __name__ == "__main__":
    print(json.dumps(check_xml(Path(sys.argv[1])), indent=2))
