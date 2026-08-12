from __future__ import annotations

import json
import re
from typing import Any


QUALITY_REVIEW_SCHEMA = "midbrain.effector_landmark_quality_review"
QUALITY_REVIEW_SCHEMA_VERSION = 1
_FIELDS = {
    "schema",
    "schema_version",
    "landmark_id",
    "verdict",
    "reason",
    "reviewed_point_ids",
}
_VERDICTS = {"PASS", "FAIL", "UNRESOLVED"}


def _require_exact_fields(value: Any) -> None:
    if not isinstance(value, dict):
        raise RuntimeError("quality-review VLM result must be an object")
    observed = set(value)
    if observed == _FIELDS:
        return
    missing = ", ".join(sorted(_FIELDS - observed)) or "none"
    unexpected = ", ".join(sorted(observed - _FIELDS)) or "none"
    raise RuntimeError(
        "quality-review fields do not match the required schema; "
        f"missing: {missing}; unexpected: {unexpected}"
    )


def _json_object(text: str) -> dict[str, Any]:
    candidate = str(text).strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", candidate, flags=re.DOTALL)
        if match is None:
            raise RuntimeError("quality-review VLM did not return a JSON object")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise RuntimeError("quality-review VLM result must be an object")
    return value


def parse_quality_review(
    text: str,
    *,
    landmark: dict[str, Any],
) -> dict[str, Any]:
    return validate_quality_review(_json_object(text), landmark=landmark)


def validate_quality_review(
    value: Any,
    *,
    landmark: dict[str, Any],
) -> dict[str, Any]:
    _require_exact_fields(value)
    if value.get("schema") != QUALITY_REVIEW_SCHEMA:
        raise RuntimeError("quality-review schema is invalid")
    if value.get("schema_version") != QUALITY_REVIEW_SCHEMA_VERSION:
        raise RuntimeError("quality-review schema version is unsupported")
    if value.get("landmark_id") != landmark.get("landmark_id"):
        raise RuntimeError("quality-review landmark ID does not match")
    verdict = str(value.get("verdict") or "").upper()
    if verdict not in _VERDICTS:
        raise RuntimeError("quality-review verdict is invalid")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("quality-review reason must be non-empty")
    point_ids = value.get("reviewed_point_ids")
    required_ids = list(landmark.get("required_point_ids") or [])
    if (
        not isinstance(point_ids, list)
        or any(not isinstance(item, str) for item in point_ids)
        or set(point_ids) != set(required_ids)
        or len(point_ids) != len(required_ids)
    ):
        raise RuntimeError("quality-review point IDs do not match the profile")
    return {
        "schema": QUALITY_REVIEW_SCHEMA,
        "schema_version": QUALITY_REVIEW_SCHEMA_VERSION,
        "landmark_id": landmark["landmark_id"],
        "verdict": verdict,
        "reason": reason.strip(),
        "reviewed_point_ids": required_ids,
    }


def build_quality_review_prompt(
    *,
    profile: dict[str, Any],
    landmark: dict[str, Any],
    raw_translation_delta_m: list[float],
    raw_translation_delta_norm_m: float,
) -> str:
    point_ids = ", ".join(str(item) for item in landmark["required_point_ids"])
    landmark_description = str(
        landmark.get("description_for_vlm")
        or f"Locate the named profile points: {point_ids}."
    )
    return (
        f"Quality-check the marked {landmark['landmark_id']} evidence for the "
        "active mounted-effector profile. Profile identity and display-name "
        "metadata are not visual classification requirements. The raw XYZ "
        "arm-root translation delta is "
        f"{[float(item) for item in raw_translation_delta_m]} m with norm "
        f"{float(raw_translation_delta_norm_m):.6f} m. Confirm that every mark "
        "is on its named physical feature and that every marked registered-depth "
        "sample belongs to the same physical surface selected in RGB. Reject "
        "marks on reflections, background, support surfaces, unrelated foreground, "
        "mixed silhouette edges, empty space, or any point outside the physical "
        "landmark geometry described by the active effector profile. The profile "
        f"description is: {landmark_description} Independently inspect "
        "the raw RGB and depth-validity "
        "views; do not trust the first model's confidence or textual reason. Use "
        "each labeled panel in the landmark review crop montage to inspect every "
        "exact crosshair center at higher scale. If any crosshair center misses "
        "its named physical feature, verdict must be FAIL even when the other "
        "points are correct. The magenta OLD base-origin "
        "circle, cyan "
        "PROPOSED base-origin diamond, orange OLD FK selected-landmark square, "
        "green PROPOSED FK selected-landmark triangle, and their connecting lines "
        "are diagnostic back-projections of the active profile's selected visual "
        "landmark. They are not necessarily the controller IK origin and must not "
        "be treated as VLM landmark selections. Do not "
        "propose replacement coordinates. "
        f"Review point IDs: {point_ids}. Return exactly one JSON object with "
        "exactly these six keys and no others: schema, schema_version, "
        "landmark_id, verdict, reason, reviewed_point_ids. Set schema exactly "
        "to midbrain.effector_landmark_quality_review, schema_version to the "
        f"integer 1, and landmark_id exactly to {landmark['landmark_id']}. "
        "verdict must be exactly PASS, FAIL, or UNRESOLVED. reason must be a "
        "non-empty string. reviewed_point_ids must contain every listed review "
        "point ID exactly once and no other values. Do not wrap the JSON in "
        "Markdown, an outer key, prose, or an explanation."
    )
