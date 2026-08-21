from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Protocol

import httpx

from .openai_responses import request_structured_response


@dataclass(frozen=True)
class VisualCandidateSelection:
    candidate_id: str
    confidence: float
    rationale: str
    model: str
    response_id: str | None
    attempt_count: int = 1


class VisualCandidateSelector(Protocol):
    def select(
        self,
        reference_paths: tuple[Path, ...],
        contact_sheet_path: Path,
        candidate_ids: tuple[str, ...],
    ) -> VisualCandidateSelection: ...


class _OpenAIResponsesVisualCandidateSelector:
    schema_name = "visual_candidate"
    instruction = "Select the best visual candidate."

    def __init__(
        self,
        model: str,
        timeout_s: float = 60.0,
        reasoning_effort: str = "low",
        backend: str = "openai.responses",
    ) -> None:
        self.backend = str(backend or "").strip().lower()
        key_name = (
            "GEMINI_API_KEY"
            if self.backend == "google.gemini"
            else "OPENAI_API_KEY"
        )
        self.key_name = key_name
        self.key = str(os.environ.get(key_name) or "").strip()
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.http = httpx.Client(timeout=float(timeout_s))

    @staticmethod
    def _image(path: Path) -> dict[str, str]:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{mime};base64,{encoded}",
            "detail": "original",
        }

    def select(
        self,
        reference_paths: tuple[Path, ...],
        contact_sheet_path: Path,
        candidate_ids: tuple[str, ...],
    ) -> VisualCandidateSelection:
        if not self.key:
            raise RuntimeError(
                f"{self.key_name} is unavailable for candidate selection"
            )
        if not candidate_ids:
            raise ValueError("candidate selection requires at least one candidate")
        schema = {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string", "enum": list(candidate_ids)},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "rationale": {"type": "string", "maxLength": 500},
            },
            "required": ["candidate_id", "confidence", "rationale"],
            "additionalProperties": False,
        }
        content: list[dict[str, str]] = [
            {"type": "input_text", "text": self.instruction}
        ]
        content.extend(self._image(path) for path in reference_paths)
        content.append(self._image(contact_sheet_path))
        structured = request_structured_response(
            self.http,
            backend=self.backend,
            key=self.key,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            content=content,
            schema_name=self.schema_name,
            schema=schema,
            operation="candidate-selection",
        )
        value = structured.value
        candidate_id = str(value["candidate_id"])
        if candidate_id not in candidate_ids:
            raise RuntimeError("candidate-selection VLM selected an unknown candidate")
        return VisualCandidateSelection(
            candidate_id=candidate_id,
            confidence=float(value["confidence"]),
            rationale=str(value["rationale"]),
            model=self.model,
            response_id=structured.response_id,
            attempt_count=structured.attempt_count,
        )

    def close(self) -> None:
        self.http.close()


class OpenAIResponsesFitCandidateSelector(_OpenAIResponsesVisualCandidateSelector):
    schema_name = "arm_base_pose_fit_candidate"
    instruction = (
        "The first image(s) are immutable CAD reference views of the robot arm base. "
        "The final image is a labeled contact sheet of independently repeated fits from "
        "the same RGB-D frame and the deterministic pixel vote over every successfully "
        "acquired mask. "
        "Cyan dots and axes are the CAD projected using each FoundationPose result; "
        "translucent red is the shared supporting mask. Select the "
        "candidate whose projected CAD best aligns with the visible cylindrical base, "
        "mounting plate, and first fixed mounting geometry. Reject alignment to upper arm "
        "links, cables, tray, or background. Native FoundationPose ranking scores are "
        "intentionally withheld because they are audit-only and must not influence this "
        "visual choice. "
        "Select exactly one listed candidate and lower confidence when no projection is "
        "geometrically convincing."
    )
