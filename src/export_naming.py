"""Consistent, human-readable names for downloaded exports."""

from __future__ import annotations

from datetime import datetime
import re


def xml_export_filename(
    project: str,
    scenario_revision_id: object,
    *,
    exported_at: datetime | None = None,
    build_id: str | None = None,
) -> str:
    """Return a download name containing revision and the actual export time.

    A few pre-versioning projects have no ``scenario_revision_id`` in their
    stored timeline.  They are explicitly labelled V0 instead of falling
    back to an ambiguous legacy name.  With ``build_id`` the name is deterministic:
    the build id replaces the timestamp so the same build always downloads under one name.
    """

    match = re.fullmatch(r"rev_(\d+)", str(scenario_revision_id or "").strip())
    version = int(match.group(1)) if match else 0
    if build_id:
        return f"{project}_V{version}_{build_id}.xml"
    timestamp = (exported_at or datetime.now().astimezone()).strftime("%Y%m%d_%H%M%S")
    return f"{project}_V{version}_{timestamp}.xml"
