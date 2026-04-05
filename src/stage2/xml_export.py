from __future__ import annotations

import math
from pathlib import Path
from urllib.request import pathname2url
from xml.etree import ElementTree as ET

from .models import FullTimeline, TimelineSegment


def _path_to_url(path: str) -> str:
    return "file://" + pathname2url(str(Path(path).resolve()))


def _sub(parent: ET.Element, tag: str, text: str = "") -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text:
        el.text = text
    return el


def _rate_el(parent: ET.Element, fps: int) -> None:
    rate = ET.SubElement(parent, "rate")
    _sub(rate, "timebase", str(fps))
    _sub(rate, "ntsc", "FALSE")


def _frames(seconds: float, fps: int) -> int:
    return math.ceil(seconds * fps)


def _audio_file_el(parent: ET.Element, seg: TimelineSegment, fps: int) -> None:
    file_el = ET.SubElement(parent, "file")
    file_el.set("id", f"file-{seg.id}")
    _sub(file_el, "name", Path(seg.wav_path).name)
    _sub(file_el, "pathurl", _path_to_url(seg.wav_path))
    _rate_el(file_el, fps)
    _sub(file_el, "duration", str(_frames(seg.duration_sec, fps)))
    media = _sub(file_el, "media")
    audio = _sub(media, "audio")
    sc = _sub(audio, "samplecharacteristics")
    _sub(sc, "depth", "16")
    _sub(sc, "samplerate", "48000")
    _sub(audio, "channelcount", "2")


def _video_file_el(parent: ET.Element, seg: TimelineSegment, fps: int,
                   width: int, height: int) -> None:
    file_el = ET.SubElement(parent, "file")
    file_el.set("id", f"file-{seg.id}")
    _sub(file_el, "name", Path(seg.placeholder_path).name)
    _sub(file_el, "pathurl", _path_to_url(seg.placeholder_path))
    _rate_el(file_el, fps)
    _sub(file_el, "duration", str(_frames(seg.duration_sec, fps)))
    media = _sub(file_el, "media")
    video = _sub(media, "video")
    sc = _sub(video, "samplecharacteristics")
    _sub(sc, "width", str(width))
    _sub(sc, "height", str(height))
    _sub(sc, "pixelaspectratio", "square")
    _rate_el(sc, fps)


def build_xml(timeline: FullTimeline) -> str:
    fps = timeline.fps
    total_frames = _frames(timeline.total_duration_sec, fps)

    root = ET.Element("xmeml")
    root.set("version", "5")

    seq = ET.SubElement(root, "sequence")
    _sub(seq, "name", timeline.project)
    _sub(seq, "duration", str(total_frames))
    _rate_el(seq, fps)

    # Timecode
    tc = _sub(seq, "timecode")
    _rate_el(tc, fps)
    _sub(tc, "string", "00:00:00:00")
    _sub(tc, "frame", "0")
    _sub(tc, "displayformat", "NDF")

    media = _sub(seq, "media")

    # ── VIDEO ──────────────────────────────────────────────
    video = _sub(media, "video")
    vfmt = _sub(video, "format")
    vsc = _sub(vfmt, "samplecharacteristics")
    _sub(vsc, "width", str(timeline.width))
    _sub(vsc, "height", str(timeline.height))
    _sub(vsc, "pixelaspectratio", "square")
    _rate_el(vsc, fps)

    vtrack = _sub(video, "track")

    # ── AUDIO ──────────────────────────────────────────────
    audio = _sub(media, "audio")
    afmt = _sub(audio, "format")
    asc = _sub(afmt, "samplecharacteristics")
    _sub(asc, "depth", "16")
    _sub(asc, "samplerate", "48000")
    atrack = _sub(audio, "track")

    clip_idx = 1
    for seg in timeline.segments:
        if seg.type == "tech":
            continue

        start_f = _frames(seg.start_sec, fps)
        end_f = _frames(seg.end_sec, fps)
        dur_f = end_f - start_f
        if dur_f <= 0:
            continue

        if seg.type == "zk" and seg.wav_path:
            ci = ET.SubElement(atrack, "clipitem")
            ci.set("id", f"clipitem-{clip_idx}")
            _sub(ci, "name", seg.id)
            _sub(ci, "duration", str(dur_f))
            _rate_el(ci, fps)
            _sub(ci, "start", str(start_f))
            _sub(ci, "end", str(end_f))
            _sub(ci, "in", "0")
            _sub(ci, "out", str(dur_f))
            # Marker with text
            marker = _sub(ci, "marker")
            _sub(marker, "name", seg.id)
            _sub(marker, "comment", seg.text[:200])
            _sub(marker, "in", "0")
            _sub(marker, "out", "-1")
            _audio_file_el(ci, seg, fps)
            st = _sub(ci, "sourcetrack")
            _sub(st, "mediatype", "audio")
            _sub(st, "trackindex", "1")
            clip_idx += 1

        elif seg.type == "snh" and seg.placeholder_path:
            ci = ET.SubElement(vtrack, "clipitem")
            ci.set("id", f"clipitem-{clip_idx}")
            label = f"{seg.speaker} [{seg.timecode}]"
            _sub(ci, "name", label)
            _sub(ci, "duration", str(dur_f))
            _rate_el(ci, fps)
            _sub(ci, "start", str(start_f))
            _sub(ci, "end", str(end_f))
            _sub(ci, "in", "0")
            _sub(ci, "out", str(dur_f))
            marker = _sub(ci, "marker")
            _sub(marker, "name", label)
            _sub(marker, "comment", seg.text[:200])
            _sub(marker, "in", "0")
            _sub(marker, "out", "-1")
            _video_file_el(ci, seg, fps, timeline.width, timeline.height)
            clip_idx += 1

    ET.indent(root, space="  ")
    header = '<?xml version="1.0" encoding="UTF-8"?>\n'
    header += '<!DOCTYPE xmeml PUBLIC "//Apple//DTD XMEML 1.0//EN" '
    header += '"http://www.apple.com/DTDs/XMEML-1.dtd">\n'
    return header + ET.tostring(root, encoding="unicode")
