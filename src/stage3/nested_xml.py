"""Build a self-contained FCP7 XML rough cut from rendered viewing files.

The exporter does not talk to Premiere.  Every viewing file is wrapped in an
``RC_SRC_*`` sequence and the final sequence contains cuts of those nested
sequences.  Replacing the media in an imported source sequence can therefore
be done once without rebuilding the rough cut.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.request import pathname2url
import wave
from xml.etree import ElementTree as ET

from stage2.models import FullTimeline, TimelineSegment

logger = logging.getLogger(__name__)

_MATCHED_STATUSES = {"", "auto_matched", "confirmed"}


@dataclass(frozen=True)
class MediaSource:
    """The rendered viewing file used by one or more matched segments."""

    id: str
    path: str
    duration: float
    fps: float = 25.0
    width: int = 1920
    height: int = 1080
    name: str = ""
    media_kind: str = "video"
    placeholder_path: str = ""

    @property
    def has_video(self) -> bool:
        return self.media_kind != "audio"

    @classmethod
    def from_value(cls, source_id: str, value: object) -> "MediaSource":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            # Accept both the compact Stage 3 catalog and Stage 2 SyncSource
            # naming so the API layer need not reshape stored metadata.
            media = value.get("media")
            raw = media if isinstance(media, Mapping) else value
            return cls(
                id=str(raw.get("id") or raw.get("source_id") or source_id),
                path=str(
                    raw.get("path") or raw.get("media_path") or raw.get("container_path") or ""
                ),
                duration=_number(raw.get("duration", raw.get("duration_sec"))),
                fps=_number(raw.get("fps"), 25.0),
                width=_integer(raw.get("width"), 1920),
                height=_integer(raw.get("height"), 1080),
                name=str(
                    raw.get("name")
                    or raw.get("proxy_sequence_name")
                    or raw.get("sequence_name")
                    or ""
                ),
                media_kind=_media_kind(raw),
                placeholder_path=str(raw.get("placeholder_path") or ""),
            )
        return cls(
            id=str(getattr(value, "id", getattr(value, "source_id", source_id))),
            path=str(
                getattr(
                    value,
                    "path",
                    getattr(value, "media_path", getattr(value, "container_path", "")),
                )
            ),
            duration=_number(getattr(value, "duration", getattr(value, "duration_sec", 0.0))),
            fps=_number(getattr(value, "fps", 25.0), 25.0),
            width=_integer(getattr(value, "width", 1920), 1920),
            height=_integer(getattr(value, "height", 1080), 1080),
            name=str(
                getattr(
                    value,
                    "name",
                    getattr(value, "proxy_sequence_name", ""),
                )
            ),
            media_kind=str(getattr(value, "media_kind", "video")),
            placeholder_path=str(getattr(value, "placeholder_path", "")),
        )


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result


def _integer(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sub(parent: ET.Element, tag: str, text: object = "") -> ET.Element:
    element = ET.SubElement(parent, tag)
    if text != "":
        element.text = str(text)
    return element


def _rate(parent: ET.Element, fps: int) -> None:
    rate = _sub(parent, "rate")
    _sub(rate, "timebase", fps)
    _sub(rate, "ntsc", "FALSE")


def _frames(seconds: float, fps: int) -> int:
    return math.ceil(seconds * fps)


def _path_url(path: str) -> str:
    return "file://" + pathname2url(str(Path(path).expanduser().resolve()))


def _safe_id(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-.")
    return token or "source"


def _display_token(value: str) -> str:
    token = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.UNICODE).strip("_.")
    return token or "SOURCE"


def _wav_params(path: str) -> tuple[int, int, int]:
    if not Path(path).is_file():
        # In Docker the XMEML path intentionally points at the macOS host and
        # cannot be opened from the container. Stage 1 TTS defaults are stable.
        return 24000, 16, 1
    try:
        with wave.open(path, "rb") as stream:
            return stream.getframerate(), stream.getsampwidth() * 8, stream.getnchannels()
    except Exception as exc:  # keep parity with the Stage 2 exporter
        logger.warning("Could not read WAV parameters path=%s error=%s", path, exc)
        return 24000, 16, 1


def _coerce_timeline(value: FullTimeline | Mapping[str, Any]) -> FullTimeline:
    if isinstance(value, FullTimeline):
        return value
    return FullTimeline.from_dict(dict(value))


def _coerce_sources(
    value: Mapping[str, object] | Sequence[object],
) -> dict[str, MediaSource]:
    raw: object = value.get("sources", value) if isinstance(value, Mapping) else value
    items: list[tuple[object, object]]
    if isinstance(raw, Mapping):
        items = list(raw.items())
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        items = []
        for item in raw:
            if isinstance(item, Mapping):
                source_id = str(item.get("source_id") or item.get("id") or "").strip()
            else:
                source_id = str(getattr(item, "source_id", getattr(item, "id", ""))).strip()
            items.append((source_id, item))
    else:
        raise ValueError("catalog sources must be a mapping or list")
    result: dict[str, MediaSource] = {}
    for key, raw_source in items:
        source_id = str(key).strip()
        if not source_id:
            raise ValueError("catalog source_id must not be empty")
        source = MediaSource.from_value(source_id, raw_source)
        if source_id in result:
            raise ValueError(f"duplicate catalog source_id: {source_id}")
        result[source_id] = source
    return result


def _media_kind(value: Mapping[str, object]) -> str:
    explicit = str(value.get("media_kind") or "").strip().lower()
    if explicit in {"audio", "video"}:
        return explicit
    path = str(
        value.get("path")
        or value.get("media_path")
        or value.get("container_path")
        or value.get("name")
        or ""
    )
    return "audio" if Path(path).suffix.lower() in {".mp3", ".wav"} else "video"


def _validate(timeline: FullTimeline, sources: Mapping[str, MediaSource]) -> None:
    if timeline.fps <= 0:
        raise ValueError("timeline fps must be positive")
    seen_segment_ids: set[str] = set()
    for source_id, source in sources.items():
        if not source.path.strip():
            raise ValueError(f"source {source_id} has no media path")
        if not math.isfinite(source.duration) or source.duration <= 0:
            raise ValueError(f"source {source_id} duration must be positive")
        if not math.isfinite(source.fps) or source.fps <= 0:
            raise ValueError(f"source {source_id} fps must be positive")
        if not float(source.fps).is_integer():
            raise ValueError(f"source {source_id} fps must be an integer for XMEML")
        if int(source.fps) != timeline.fps:
            raise ValueError(
                f"source {source_id} fps ({source.fps:g}) does not match timeline fps "
                f"({timeline.fps})"
            )
        if source.width <= 0 or source.height <= 0:
            raise ValueError(f"source {source_id} dimensions must be positive")
        if not source.has_video and not source.placeholder_path.strip():
            raise ValueError(f"audio source {source_id} has no placeholder card")

    previous_end = 0.0
    for segment in timeline.segments:
        if segment.id in seen_segment_ids:
            raise ValueError(f"duplicate timeline segment id: {segment.id}")
        seen_segment_ids.add(segment.id)
        for label, value in (
            ("start", segment.start_sec),
            ("duration", segment.duration_sec),
            ("source in", segment.source_in_sec),
            ("source out", segment.source_out_sec),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"segment {segment.id} {label} must be non-negative")
        if segment.start_sec + 1e-9 < previous_end and segment.type != "tech":
            raise ValueError(f"segment {segment.id} overlaps the previous timeline segment")
        if segment.type != "tech":
            previous_end = segment.end_sec

        has_range = segment.source_out_sec > segment.source_in_sec
        matched = (
            segment.type in {"snh", "host"}
            and segment.match_status in _MATCHED_STATUSES
            and bool(segment.source_id)
            and has_range
        )
        if segment.match_status in {"auto_matched", "confirmed"} and not matched:
            raise ValueError(f"matched segment {segment.id} has no valid source range")
        if not matched:
            continue
        matched_source = sources.get(segment.source_id)
        if matched_source is None:
            raise ValueError(f"segment {segment.id} references unknown source {segment.source_id}")
        tolerance = 1.0 / timeline.fps
        if segment.source_out_sec > matched_source.duration + tolerance:
            raise ValueError(f"segment {segment.id} source range exceeds media duration")
        range_duration = segment.source_out_sec - segment.source_in_sec
        if abs(range_duration - segment.duration_sec) > tolerance:
            raise ValueError(f"segment {segment.id} duration does not match its source range")


def _sequence_header(
    parent: ET.Element,
    *,
    sequence_id: str,
    name: str,
    duration: int,
    fps: int,
) -> ET.Element:
    sequence = ET.SubElement(parent, "sequence", {"id": sequence_id})
    _sub(sequence, "name", name)
    _sub(sequence, "duration", duration)
    _rate(sequence, fps)
    timecode = _sub(sequence, "timecode")
    _rate(timecode, fps)
    _sub(timecode, "string", "00:00:00:00")
    _sub(timecode, "frame", "0")
    _sub(timecode, "displayformat", "NDF")
    return sequence


def _video_format(video: ET.Element, width: int, height: int, fps: int) -> None:
    fmt = _sub(video, "format")
    characteristics = _sub(fmt, "samplecharacteristics")
    _sub(characteristics, "width", width)
    _sub(characteristics, "height", height)
    _sub(characteristics, "pixelaspectratio", "square")
    _rate(characteristics, fps)


def _audio_format(audio: ET.Element) -> None:
    fmt = _sub(audio, "format")
    characteristics = _sub(fmt, "samplecharacteristics")
    _sub(characteristics, "depth", "16")
    _sub(characteristics, "samplerate", "48000")


def _media_file(
    parent: ET.Element,
    source: MediaSource,
    file_id: str,
    duration_frames: int,
) -> None:
    fps = int(source.fps)
    file_element = ET.SubElement(parent, "file", {"id": file_id})
    _sub(file_element, "name", Path(source.path).name)
    _sub(file_element, "pathurl", _path_url(source.path))
    _rate(file_element, fps)
    _sub(file_element, "duration", duration_frames)
    media = _sub(file_element, "media")
    if source.has_video:
        video = _sub(media, "video")
        characteristics = _sub(video, "samplecharacteristics")
        _sub(characteristics, "width", source.width)
        _sub(characteristics, "height", source.height)
        _sub(characteristics, "pixelaspectratio", "square")
        _rate(characteristics, fps)
    audio = _sub(media, "audio")
    _sub(audio, "channelcount", "2")


def _source_sequence(
    parent: ET.Element,
    source: MediaSource,
    sequence_id: str,
    file_id: str,
    sequence_name: str,
) -> None:
    fps = int(source.fps)
    duration = _frames(source.duration, fps)
    sequence = _sequence_header(
        parent,
        sequence_id=sequence_id,
        name=sequence_name,
        duration=duration,
        fps=fps,
    )
    media = _sub(sequence, "media")
    video = _sub(media, "video")
    _video_format(video, source.width, source.height, fps)
    video_track = _sub(video, "track")
    video_clip = ET.SubElement(video_track, "clipitem", {"id": f"clip-{file_id}-v"})
    video_path = source.path if source.has_video else source.placeholder_path
    _sub(video_clip, "name", Path(video_path).name)
    _sub(video_clip, "duration", duration)
    _rate(video_clip, fps)
    _sub(video_clip, "start", 0)
    _sub(video_clip, "end", duration)
    _sub(video_clip, "in", 0)
    _sub(video_clip, "out", duration)
    if source.has_video:
        _media_file(video_clip, source, file_id, duration)
    else:
        card_file = ET.SubElement(video_clip, "file", {"id": f"card-{file_id}"})
        _sub(card_file, "name", Path(video_path).name)
        _sub(card_file, "pathurl", _path_url(video_path))
        _rate(card_file, fps)
        _sub(card_file, "duration", duration)
        card_media = _sub(card_file, "media")
        card_video = _sub(card_media, "video")
        characteristics = _sub(card_video, "samplecharacteristics")
        _sub(characteristics, "width", source.width)
        _sub(characteristics, "height", source.height)
        _sub(characteristics, "pixelaspectratio", "square")
        _rate(characteristics, fps)
    source_track = _sub(video_clip, "sourcetrack")
    _sub(source_track, "mediatype", "video")
    _sub(source_track, "trackindex", 1)

    audio = _sub(media, "audio")
    _audio_format(audio)
    audio_track = _sub(audio, "track")
    audio_clip = ET.SubElement(audio_track, "clipitem", {"id": f"clip-{file_id}-a"})
    _sub(audio_clip, "name", Path(source.path).name)
    _sub(audio_clip, "duration", duration)
    _rate(audio_clip, fps)
    _sub(audio_clip, "start", 0)
    _sub(audio_clip, "end", duration)
    _sub(audio_clip, "in", 0)
    _sub(audio_clip, "out", duration)
    if source.has_video:
        ET.SubElement(audio_clip, "file", {"id": file_id})
    else:
        _media_file(audio_clip, source, file_id, duration)
    source_track = _sub(audio_clip, "sourcetrack")
    _sub(source_track, "mediatype", "audio")
    _sub(source_track, "trackindex", 1)


def _marker(parent: ET.Element, segment: TimelineSegment, name: str) -> None:
    marker = _sub(parent, "marker")
    _sub(marker, "name", name)
    _sub(marker, "comment", segment.text[:200])
    _sub(marker, "in", 0)
    _sub(marker, "out", -1)


def _revision_label(parent: ET.Element, segment: TimelineSegment) -> None:
    if segment.revision_status not in {"", "unchanged"}:
        labels = _sub(parent, "labels")
        # Premiere's built-in red/pink label is named "Rose" in FCP7 XML.
        _sub(labels, "label2", "Rose")


def _nested_clip(
    track: ET.Element,
    segment: TimelineSegment,
    *,
    clip_id: str,
    source: MediaSource,
    sequence_id: str,
    sequence_name: str,
    media_type: str,
    timeline_fps: int,
) -> None:
    source_fps = int(source.fps)
    start = _frames(segment.start_sec, timeline_fps)
    end = max(_frames(segment.end_sec, timeline_fps), start + 1)
    source_in = _frames(segment.source_in_sec, source_fps)
    source_out = _frames(segment.source_out_sec, source_fps)
    clip = ET.SubElement(track, "clipitem", {"id": clip_id})
    _sub(clip, "name", sequence_name)
    _sub(clip, "duration", _frames(source.duration, source_fps))
    _rate(clip, source_fps)
    _sub(clip, "start", start)
    _sub(clip, "end", end)
    _sub(clip, "in", source_in)
    _sub(clip, "out", source_out)
    _marker(clip, segment, segment.id)
    _revision_label(clip, segment)
    ET.SubElement(clip, "sequence", {"id": sequence_id})
    source_track = _sub(clip, "sourcetrack")
    _sub(source_track, "mediatype", media_type)
    _sub(source_track, "trackindex", 1)


def _placeholder_clip(
    track: ET.Element,
    segment: TimelineSegment,
    *,
    clip_id: str,
    timeline: FullTimeline,
) -> None:
    start = _frames(segment.start_sec, timeline.fps)
    end = max(_frames(segment.end_sec, timeline.fps), start + 1)
    duration = end - start
    label = f"RC_BLOCK {segment.block_uid} | {segment.speaker} [{segment.timecode}]"
    clip = ET.SubElement(track, "clipitem", {"id": clip_id})
    _sub(clip, "name", label)
    _sub(clip, "duration", duration)
    _rate(clip, timeline.fps)
    _sub(clip, "start", start)
    _sub(clip, "end", end)
    _sub(clip, "in", 0)
    _sub(clip, "out", duration)
    _marker(clip, segment, label)
    _revision_label(clip, segment)
    file_element = ET.SubElement(clip, "file", {"id": f"file-{clip_id}"})
    _sub(file_element, "name", Path(segment.placeholder_path).name)
    _sub(file_element, "pathurl", _path_url(segment.placeholder_path))
    _rate(file_element, timeline.fps)
    _sub(file_element, "duration", duration)
    media = _sub(file_element, "media")
    video = _sub(media, "video")
    characteristics = _sub(video, "samplecharacteristics")
    _sub(characteristics, "width", timeline.width)
    _sub(characteristics, "height", timeline.height)
    _sub(characteristics, "pixelaspectratio", "square")
    _rate(characteristics, timeline.fps)


def _voiceover_clip(
    track: ET.Element,
    segment: TimelineSegment,
    *,
    clip_id: str,
    fps: int,
) -> None:
    start = _frames(segment.start_sec, fps)
    end = max(_frames(segment.end_sec, fps), start + 1)
    duration = end - start
    clip = ET.SubElement(track, "clipitem", {"id": clip_id})
    _sub(clip, "name", segment.id)
    _sub(clip, "duration", duration)
    _rate(clip, fps)
    _sub(clip, "start", start)
    _sub(clip, "end", end)
    _sub(clip, "in", 0)
    _sub(clip, "out", duration)
    _marker(clip, segment, segment.id)
    _revision_label(clip, segment)
    file_element = ET.SubElement(clip, "file", {"id": f"file-{clip_id}"})
    _sub(file_element, "name", Path(segment.wav_path).name)
    _sub(file_element, "pathurl", _path_url(segment.wav_path))
    _rate(file_element, fps)
    _sub(file_element, "duration", duration)
    sample_rate, depth, channels = _wav_params(segment.wav_path)
    media = _sub(file_element, "media")
    audio = _sub(media, "audio")
    characteristics = _sub(audio, "samplecharacteristics")
    _sub(characteristics, "depth", depth)
    _sub(characteristics, "samplerate", sample_rate)
    _sub(audio, "channelcount", channels)
    source_track = _sub(clip, "sourcetrack")
    _sub(source_track, "mediatype", "audio")
    _sub(source_track, "trackindex", 1)


def build_nested_xml(
    timeline: FullTimeline | Mapping[str, Any],
    catalog: Mapping[str, object] | Sequence[object],
) -> str:
    """Return an XMEML v5 project containing viewing and final sequences."""

    full_timeline = _coerce_timeline(timeline)
    sources = _coerce_sources(catalog)
    _validate(full_timeline, sources)

    # Every rendered viewing belongs in RC_VIEWINGS, including files without a
    # selected quote yet. Entries pointing at the same physical file share one
    # RC_SRC sequence.
    sequence_by_source: dict[str, tuple[MediaSource, str, str, str]] = {}
    by_path: dict[str, tuple[MediaSource, str, str, str]] = {}
    used_xml_ids: set[str] = set()
    used_sequence_names: set[str] = set()
    for source_id in sorted(sources):
        source = sources[source_id]
        media_key = str(Path(source.path).expanduser().resolve())
        entry = by_path.get(media_key)
        if entry is None:
            base_id = f"sequence-rc-src-{_safe_id(source_id)}"
            sequence_id = base_id
            suffix = 2
            while sequence_id in used_xml_ids:
                sequence_id = f"{base_id}-{suffix}"
                suffix += 1
            used_xml_ids.add(sequence_id)
            desired_name = source.name.strip()
            if desired_name and Path(desired_name).suffix:
                desired_name = Path(desired_name).stem
            base_name = (
                desired_name
                if desired_name.startswith("RC_SRC_")
                else f"RC_SRC_{_display_token(desired_name or Path(source.path).stem)}"
            )
            sequence_name = base_name
            name_suffix = 2
            while sequence_name in used_sequence_names:
                sequence_name = f"{base_name}_{name_suffix}"
                name_suffix += 1
            used_sequence_names.add(sequence_name)
            entry = (source, sequence_id, f"file-{sequence_id}", sequence_name)
            by_path[media_key] = entry
        else:
            canonical = entry[0]
            if (
                abs(canonical.duration - source.duration) > 1.0 / full_timeline.fps
                or canonical.fps != source.fps
                or canonical.width != source.width
                or canonical.height != source.height
                or canonical.media_kind != source.media_kind
                or canonical.placeholder_path != source.placeholder_path
            ):
                raise ValueError(
                    f"catalog entries for {source.path} have inconsistent media metadata"
                )
        sequence_by_source[source_id] = entry

    root = ET.Element("xmeml", {"version": "5"})
    project = _sub(root, "project")
    _sub(project, "name", full_timeline.project)
    children = _sub(project, "children")
    viewing_bin = _sub(children, "bin")
    _sub(viewing_bin, "name", "RC_VIEWINGS")
    bin_children = _sub(viewing_bin, "children")
    for source, sequence_id, file_id, sequence_name in by_path.values():
        _source_sequence(bin_children, source, sequence_id, file_id, sequence_name)

    total_seconds = max(
        (segment.end_sec for segment in full_timeline.segments if segment.type != "tech"),
        default=0.0,
    )
    total_frames = _frames(total_seconds, full_timeline.fps)
    final = _sequence_header(
        children,
        sequence_id="sequence-rough-cut",
        name=f"ROUGH_CUT_{full_timeline.project}",
        duration=total_frames,
        fps=full_timeline.fps,
    )
    media = _sub(final, "media")
    video = _sub(media, "video")
    _video_format(video, full_timeline.width, full_timeline.height, full_timeline.fps)
    video_track = _sub(video, "track")
    audio = _sub(media, "audio")
    _audio_format(audio)
    source_audio_track = _sub(audio, "track")
    voiceover_track = _sub(audio, "track")

    clip_number = 0
    for segment in full_timeline.segments:
        if segment.type == "tech" or segment.duration_sec <= 0:
            continue
        entry = sequence_by_source.get(segment.source_id)
        is_nested = entry is not None and (
            segment.type in {"snh", "host"}
            and segment.match_status in _MATCHED_STATUSES
            and segment.source_out_sec > segment.source_in_sec
        )
        if is_nested and entry is not None:
            source, sequence_id, _, sequence_name = entry
            clip_number += 1
            _nested_clip(
                video_track,
                segment,
                clip_id=f"clip-final-{clip_number}-v",
                source=source,
                sequence_id=sequence_id,
                sequence_name=sequence_name,
                media_type="video",
                timeline_fps=full_timeline.fps,
            )
            _nested_clip(
                source_audio_track,
                segment,
                clip_id=f"clip-final-{clip_number}-a",
                source=source,
                sequence_id=sequence_id,
                sequence_name=sequence_name,
                media_type="audio",
                timeline_fps=full_timeline.fps,
            )
        elif segment.type in {"snh", "host"} and segment.placeholder_path:
            clip_number += 1
            _placeholder_clip(
                video_track,
                segment,
                clip_id=f"clip-final-{clip_number}-placeholder",
                timeline=full_timeline,
            )
        elif segment.type == "zk" and segment.wav_path:
            clip_number += 1
            _voiceover_clip(
                voiceover_track,
                segment,
                clip_id=f"clip-final-{clip_number}-zk",
                fps=full_timeline.fps,
            )

    ET.indent(root, space="  ")
    declaration = '<?xml version="1.0" encoding="UTF-8"?>\n'
    doctype = (
        '<!DOCTYPE xmeml PUBLIC "//Apple//DTD XMEML 1.0//EN" '
        '"http://www.apple.com/DTDs/XMEML-1.dtd">\n'
    )
    return declaration + doctype + ET.tostring(root, encoding="unicode")


# A compact alias is useful to callers that select the exporter module first.
build_xml = build_nested_xml
