"""Shared helpers for checking scenario/timeline freshness across stages.

Used by both stage2 and stage3 route modules to decide whether a stored
artifact (sync matches, timelines) is still valid against the current
scenario/normalization revision.
"""

from __future__ import annotations


def _scenario_revision_matches(stored: object, current: object) -> bool:
    """Compare scenario identities while accepting artifacts from legacy files.

    An empty ID on either side means that the artifact predates scenario-level
    versioning. Explicit, different IDs are always stale.
    """
    stored_id = str(stored or "").strip()
    current_id = str(current or "").strip()
    return not stored_id or not current_id or stored_id == current_id


def _timeline_is_current(timeline: dict, normalization: dict) -> bool:
    return timeline.get("normalization_revision") == normalization.get(
        "revision"
    ) and _scenario_revision_matches(
        timeline.get("scenario_revision_id"),
        normalization.get("scenario_revision_id"),
    )
