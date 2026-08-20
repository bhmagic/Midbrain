from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class StructuredResponse:
    value: dict[str, Any]
    response_id: str | None
    attempt_count: int


def request_structured_response(
    http: httpx.Client,
    *,
    backend: str = "openai.responses",
    key: str,
    model: str,
    reasoning_effort: str,
    content: list[dict[str, Any]],
    schema_name: str,
    schema: dict[str, Any],
    operation: str,
    maximum_attempts: int = 2,
) -> StructuredResponse:
    """Request strict JSON from a configured VLM and retry incomplete responses."""
    backend = str(backend or "").strip().lower()
    if backend not in {"openai.responses", "google.gemini"}:
        raise ValueError(f"unsupported structured VLM backend: {backend!r}")
    last_problem = "unknown response state"
    maximum_attempts = max(1, int(maximum_attempts))
    for attempt in range(1, maximum_attempts + 1):
        try:
            if backend == "google.gemini":
                response = _post_gemini(
                    http,
                    key=key,
                    model=model,
                    content=content,
                    schema=schema,
                )
            else:
                response = _post_openai(
                    http,
                    key=key,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    content=content,
                    schema_name=schema_name,
                    schema=schema,
                    attempt=attempt,
                )
            response.raise_for_status()
        except httpx.TransportError as error:
            last_problem = f"transient transport error: {error}"
            continue
        except httpx.HTTPStatusError as error:
            status_code = error.response.status_code
            if status_code in {408, 409, 425, 429} or status_code >= 500:
                last_problem = f"transient HTTP {status_code}"
                continue
            raise
        payload = response.json()
        if backend == "google.gemini":
            response_id, texts, refusals, status, reason = _gemini_output(payload)
        else:
            response_id, texts, refusals, status, reason = _openai_output(payload)
        combined = "".join(texts).strip()
        if combined:
            try:
                value = json.loads(combined)
            except json.JSONDecodeError as error:
                last_problem = (
                    f"invalid JSON at character {error.pos}; response_id={response_id or 'none'}"
                )
            else:
                if isinstance(value, dict):
                    return StructuredResponse(value, response_id, attempt)
                last_problem = (
                    f"structured output was not an object; response_id={response_id or 'none'}"
                )
        else:
            last_problem = (
                f"status={status}, reason={reason}, response_id={response_id or 'none'}"
            )
    raise RuntimeError(
        f"{operation} VLM returned no usable structured output after "
        f"{maximum_attempts} attempts ({last_problem})"
    )


def _post_openai(
    http: httpx.Client,
    *,
    key: str,
    model: str,
    reasoning_effort: str,
    content: list[dict[str, Any]],
    schema_name: str,
    schema: dict[str, Any],
    attempt: int,
) -> httpx.Response:
    return http.post(
        "https://api.openai.com/v1/responses",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "reasoning": {"effort": reasoning_effort},
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            # The limit includes hidden reasoning tokens and visible JSON.
            "max_output_tokens": 3000 * attempt,
            "store": False,
        },
    )


def _post_gemini(
    http: httpx.Client,
    *,
    key: str,
    model: str,
    content: list[dict[str, Any]],
    schema: dict[str, Any],
) -> httpx.Response:
    parts: list[dict[str, Any]] = []
    for item in content:
        item_type = str(item.get("type") or "")
        if item_type == "input_text":
            parts.append({"text": str(item.get("text") or "")})
            continue
        if item_type != "input_image":
            raise ValueError(f"unsupported Gemini content item: {item_type!r}")
        image_url = str(item.get("image_url") or "")
        prefix, separator, encoded = image_url.partition(",")
        if not separator or not prefix.startswith("data:") or ";base64" not in prefix:
            raise ValueError("Gemini image content must be a base64 data URL")
        mime_type = prefix[5:].split(";", 1)[0]
        base64.b64decode(encoded, validate=True)
        parts.append(
            {
                "inlineData": {
                    "mimeType": mime_type,
                    "data": encoded,
                }
            }
        )
    return http.post(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent",
        headers={
            "x-goog-api-key": key,
            "Content-Type": "application/json",
        },
        json={
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "temperature": 0.2,
                "thinkingConfig": {"thinkingBudget": 0},
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
            },
        },
    )


def _openai_output(
    payload: dict[str, Any],
) -> tuple[str | None, list[str], list[str], str, str]:
    response_id = str(payload.get("id") or "") or None
    texts: list[str] = []
    refusals: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text":
                texts.append(str(part.get("text") or ""))
            elif part.get("type") == "refusal":
                refusals.append(str(part.get("refusal") or ""))
    status = str(payload.get("status") or "unknown")
    incomplete = payload.get("incomplete_details")
    incomplete = incomplete if isinstance(incomplete, dict) else {}
    error = payload.get("error")
    error = error if isinstance(error, dict) else {}
    reason = str(
        incomplete.get("reason")
        or error.get("message")
        or ("; ".join(value for value in refusals if value))
        or "no output_text"
    )
    return response_id, texts, refusals, status, reason


def _gemini_output(
    payload: dict[str, Any],
) -> tuple[str | None, list[str], list[str], str, str]:
    response_id = str(payload.get("responseId") or "") or None
    texts: list[str] = []
    refusals: list[str] = []
    finish_reasons: list[str] = []
    for candidate in payload.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        finish_reasons.append(str(candidate.get("finishReason") or ""))
        candidate_content = candidate.get("content")
        candidate_content = (
            candidate_content if isinstance(candidate_content, dict) else {}
        )
        for part in candidate_content.get("parts") or []:
            if isinstance(part, dict) and "text" in part:
                texts.append(str(part.get("text") or ""))
    prompt_feedback = payload.get("promptFeedback")
    prompt_feedback = prompt_feedback if isinstance(prompt_feedback, dict) else {}
    block_reason = str(prompt_feedback.get("blockReason") or "")
    if block_reason:
        refusals.append(block_reason)
    status = ",".join(value for value in finish_reasons if value) or "unknown"
    reason = "; ".join(value for value in refusals if value) or "no output text"
    return response_id, texts, refusals, status, reason
