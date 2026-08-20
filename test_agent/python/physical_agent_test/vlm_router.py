from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
import uuid
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx

try:
    from google import genai
    from google.genai import types
except ModuleNotFoundError:
    genai = None
    types = None

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None

from .phase4_policy import report_operation_progress


_selected_vlm_model: ContextVar[str | None] = ContextVar(
    "selected_vlm_model",
    default=None,
)
_active_client_request_id: ContextVar[str | None] = ContextVar(
    "active_vlm_client_request_id",
    default=None,
)


def set_vlm_model_selection(model_id: str | None) -> Token:
    normalized = str(model_id or "").strip() or None
    return _selected_vlm_model.set(normalized)


def reset_vlm_model_selection(token: Token) -> None:
    _selected_vlm_model.reset(token)


def get_vlm_model_selection() -> str | None:
    return _selected_vlm_model.get()


@dataclass(frozen=True)
class BackendInference:
    text: str
    server_request_id: str | None = None
    response_id: str | None = None


class VisionLanguageBackend(Protocol):
    backend_id: str
    model_id: str

    def generate(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> str | BackendInference:
        """Run one synchronous VLM inference."""

    def generate_images(
        self,
        images: list[tuple[bytes, str]],
        prompt: str,
    ) -> str | BackendInference:
        """Run one synchronous inference over multiple labeled prompt images."""


@dataclass(frozen=True)
class VlmInferenceResult:
    text: str
    backend_id: str
    model_id: str
    attempt_count: int
    failed_attempts: tuple[dict[str, object], ...]
    quality_control_mode: str
    elapsed_ms: float
    input_sha256: str
    input_bytes: int
    mime_type: str
    request_id: str | None = None
    client_request_id: str | None = None
    server_request_id: str | None = None
    response_id: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)

class GeminiVisionLanguageBackend:
    backend_id = "google.gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        request_timeout_s: float = 45.0,
    ):
        if genai is None or types is None:
            raise RuntimeError(
                "google-genai is required for the Gemini VLM backend"
            )
        self.model_id = model_id
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=max(1, int(float(request_timeout_s) * 1000.0))
            ),
        )

    def generate(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> str:
        return self.generate_images([(image_bytes, mime_type)], prompt)

    def generate_images(
        self,
        images: list[tuple[bytes, str]],
        prompt: str,
    ) -> str:
        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
            for image_bytes, mime_type in images
        ]
        contents.append(prompt)
        response = self._client.models.generate_content(
            model=self.model_id,
            contents=contents,
            config=types.GenerateContentConfig(
                temperature=0.2,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = response.text
        if not text:
            raise RuntimeError("VLM returned no text")
        return text.strip()


class OpenAIVisionLanguageBackend:
    backend_id = "openai.responses"

    def __init__(
        self,
        *,
        api_key: str,
        model_id: str,
        request_timeout_s: float = 45.0,
    ):
        if OpenAI is None:
            raise RuntimeError(
                "openai is required for the OpenAI VLM backend"
            )
        self.model_id = model_id
        self._client = OpenAI(
            api_key=api_key,
            timeout=max(1.0, float(request_timeout_s)),
            max_retries=0,
        )

    def generate(
        self,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> BackendInference:
        return self.generate_images([(image_bytes, mime_type)], prompt)

    def generate_images(
        self,
        images: list[tuple[bytes, str]],
        prompt: str,
    ) -> BackendInference:
        content = [
            {
                "type": "input_text",
                "text": prompt,
            }
        ]
        for image_bytes, mime_type in images:
            encoded = base64.b64encode(image_bytes).decode("ascii")
            content.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{mime_type};base64,{encoded}",
                    "detail": "high",
                }
            )
        request_id = _active_client_request_id.get()
        request_options: dict[str, object] = {
            "model": self.model_id,
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "max_output_tokens": 1200,
        }
        if request_id is not None:
            request_options["extra_headers"] = {
                "X-Client-Request-Id": request_id,
            }
        response = self._client.responses.create(
            **request_options,
        )
        text = response.output_text
        if not text:
            raise RuntimeError("OpenAI VLM returned no text")
        return BackendInference(
            text=text.strip(),
            server_request_id=str(getattr(response, "_request_id", "") or "")
            or None,
            response_id=str(getattr(response, "id", "") or "") or None,
        )


class VisionLanguageRouter:
    """Ordered VLM routing with fallback; voting remains explicitly disabled."""

    def __init__(
        self,
        backends: list[VisionLanguageBackend],
        *,
        maximum_attempts: int | None = None,
        attempts_per_backend: int = 2,
        retry_backoff_s: float = 0.25,
        quality_control_mode: str = "OFF_FUTURE",
        attempt_timeout_s: float = 45.0,
    ):
        if not backends:
            raise ValueError("at least one VLM backend is required")
        self.backends = list(backends)
        self.maximum_attempts = min(
            len(self.backends),
            max(1, int(maximum_attempts or len(self.backends))),
        )
        self.attempts_per_backend = int(attempts_per_backend)
        self.retry_backoff_s = float(retry_backoff_s)
        self.quality_control_mode = str(quality_control_mode)
        self.attempt_timeout_s = float(attempt_timeout_s)
        if not 1 <= self.attempts_per_backend <= 3:
            raise ValueError("VLM attempts per backend must be between 1 and 3")
        if not 0.0 <= self.retry_backoff_s <= 5.0:
            raise ValueError("VLM retry backoff must be between 0 and 5 seconds")
        if self.attempt_timeout_s <= 0.0:
            raise ValueError("VLM attempt timeout must be positive")
        if self.quality_control_mode != "OFF_FUTURE":
            raise ValueError("VLM voting and quality-control modes are not enabled yet")

    async def generate(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
        request_id: str | None = None,
    ) -> VlmInferenceResult:
        return await self.generate_images(
            images=[(image_bytes, mime_type)],
            prompt=prompt,
            request_id=request_id,
        )

    async def generate_images(
        self,
        *,
        images: list[tuple[bytes, str]],
        prompt: str,
        request_id: str | None = None,
    ) -> VlmInferenceResult:
        if not isinstance(images, list) or not 1 <= len(images) <= 8:
            raise ValueError("VLM input must contain one to eight images")
        normalized_images: list[tuple[bytes, str]] = []
        digest = hashlib.sha256()
        input_bytes = 0
        for image_bytes, mime_type in images:
            data = bytes(image_bytes)
            media = str(mime_type).strip()
            if not data or not media.startswith("image/"):
                raise ValueError("every VLM input must be a non-empty image")
            encoded_media = media.encode("utf-8")
            digest.update(len(encoded_media).to_bytes(4, "big"))
            digest.update(encoded_media)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            input_bytes += len(data)
            normalized_images.append((data, media))
        started = time.monotonic()
        input_sha256 = (
            hashlib.sha256(normalized_images[0][0]).hexdigest()
            if len(normalized_images) == 1
            else digest.hexdigest()
        )
        failures: list[dict[str, object]] = []
        logical_request_id = self._normalize_request_id(request_id)
        selected_model = _selected_vlm_model.get()
        backends = self.backends
        maximum_attempts = self.maximum_attempts
        if selected_model is not None:
            backends = [
                backend
                for backend in self.backends
                if backend.model_id == selected_model
            ]
            if not backends:
                raise ValueError(
                    f"selected VLM model is unavailable: {selected_model}"
                )
            maximum_attempts = 1
        attempt_count = 0
        for backend in backends[:maximum_attempts]:
            for backend_attempt in range(1, self.attempts_per_backend + 1):
                attempt_count += 1
                client_request_id = (
                    f"{logical_request_id}/attempt-{attempt_count:02d}"
                )
                report_operation_progress(
                    f"VLM_ATTEMPT_{attempt_count}_{backend.backend_id}"
                )
                try:
                    backend_call = (
                        backend.generate
                        if len(normalized_images) == 1
                        else getattr(backend, "generate_images", None)
                    )
                    if backend_call is None:
                        raise RuntimeError(
                            f"VLM backend {backend.backend_id} does not support multiple images"
                        )
                    call_arguments = (
                        (
                            normalized_images[0][0],
                            normalized_images[0][1],
                            prompt,
                        )
                        if len(normalized_images) == 1
                        else (normalized_images, prompt)
                    )
                    request_token = _active_client_request_id.set(
                        client_request_id
                    )
                    try:
                        backend_result = await self._await_backend(
                            asyncio.to_thread(backend_call, *call_arguments),
                            attempt=attempt_count,
                            backend_id=backend.backend_id,
                        )
                    finally:
                        _active_client_request_id.reset(request_token)
                    if isinstance(backend_result, BackendInference):
                        text = backend_result.text
                        server_request_id = backend_result.server_request_id
                        response_id = backend_result.response_id
                    else:
                        text = str(backend_result)
                        server_request_id = None
                        response_id = None
                    report_operation_progress(
                        f"VLM_ATTEMPT_{attempt_count}_COMPLETED"
                    )
                    return VlmInferenceResult(
                        text=text,
                        backend_id=backend.backend_id,
                        model_id=backend.model_id,
                        attempt_count=attempt_count,
                        failed_attempts=tuple(failures),
                        quality_control_mode=self.quality_control_mode,
                        elapsed_ms=(time.monotonic() - started) * 1000.0,
                        input_sha256=input_sha256,
                        input_bytes=input_bytes,
                        mime_type=(
                            normalized_images[0][1]
                            if len(normalized_images) == 1
                            else "multipart/mixed"
                        ),
                        request_id=logical_request_id,
                        client_request_id=client_request_id,
                        server_request_id=server_request_id,
                        response_id=response_id,
                    )
                except Exception as error:
                    retryable = self._is_retryable_failure(error)
                    error_text = (
                        f"timeout after {self.attempt_timeout_s:.3f}s"
                        if isinstance(error, TimeoutError)
                        else str(error)
                    )
                    failures.append(
                        {
                            "backend_id": backend.backend_id,
                            "model_id": backend.model_id,
                            "attempt": attempt_count,
                            "backend_attempt": backend_attempt,
                            "client_request_id": client_request_id,
                            "retryable": retryable,
                            "error": error_text,
                        }
                    )
                    if (
                        not retryable
                        or backend_attempt >= self.attempts_per_backend
                    ):
                        break
                    report_operation_progress(
                        f"VLM_ATTEMPT_{attempt_count}_RETRY_WAIT"
                    )
                    if self.retry_backoff_s > 0.0:
                        await asyncio.sleep(self.retry_backoff_s)
        raise RuntimeError(
            "all configured VLM backends failed: "
            + "; ".join(
                f"{failure['backend_id']}/{failure['model_id']}: "
                f"{failure['error']}"
                for failure in failures
            )
        )

    @staticmethod
    def _normalize_request_id(request_id: str | None) -> str:
        value = str(request_id or "").strip()
        if not value:
            value = f"midbrain-vlm-{uuid.uuid4().hex}"
        try:
            value.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError(
                "VLM request_id must contain only ASCII characters"
            ) from error
        if len(value) > 480:
            raise ValueError("VLM request_id must contain at most 480 characters")
        return value

    @staticmethod
    def _is_retryable_failure(error: Exception) -> bool:
        candidate: BaseException | None = error
        visited: set[int] = set()
        while candidate is not None and id(candidate) not in visited:
            visited.add(id(candidate))
            if isinstance(
                candidate,
                (
                    TimeoutError,
                    ConnectionError,
                    httpx.TimeoutException,
                    httpx.TransportError,
                ),
            ):
                return True
            status_code = getattr(candidate, "status_code", None)
            if status_code is None:
                status_code = getattr(candidate, "code", None)
            try:
                normalized_status = int(status_code)
            except (TypeError, ValueError):
                normalized_status = None
            if normalized_status in {408, 409, 425, 429, 500, 502, 503, 504}:
                return True
            candidate = candidate.__cause__ or candidate.__context__
        return False

    async def _await_backend(
        self,
        awaitable,
        *,
        attempt: int,
        backend_id: str,
    ) -> str:
        task = asyncio.ensure_future(awaitable)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.attempt_timeout_s
        try:
            while True:
                remaining_s = deadline - loop.time()
                if remaining_s <= 0.0:
                    task.cancel()
                    raise TimeoutError(
                        f"timeout after {self.attempt_timeout_s:.3f}s"
                    )
                done, _ = await asyncio.wait(
                    {task},
                    timeout=min(5.0, remaining_s),
                )
                if task in done:
                    return task.result()
                report_operation_progress(
                    f"VLM_ATTEMPT_{attempt}_{backend_id}_AWAITING_RESPONSE"
                )
        except asyncio.CancelledError:
            task.cancel()
            raise


def build_default_vlm_router(
    *,
    gemini_model: str,
    attempt_timeout_s: float,
    attempts_per_backend: int = 2,
    retry_backoff_s: float = 0.25,
) -> VisionLanguageRouter:
    backends: list[VisionLanguageBackend] = []
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_api_key and gemini_model.strip():
        backends.append(
            GeminiVisionLanguageBackend(
                api_key=gemini_api_key,
                model_id=gemini_model,
                request_timeout_s=attempt_timeout_s,
            )
        )
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_VLM_MODEL", "gpt-5.6-terra").strip()
    if openai_api_key and openai_model:
        backends.append(
            OpenAIVisionLanguageBackend(
                api_key=openai_api_key,
                model_id=openai_model,
                request_timeout_s=attempt_timeout_s,
            )
        )
    if not backends:
        raise RuntimeError(
            "no VLM backend is configured; set GEMINI_API_KEY or OPENAI_API_KEY"
        )
    return VisionLanguageRouter(
        backends,
        attempt_timeout_s=attempt_timeout_s,
        attempts_per_backend=attempts_per_backend,
        retry_backoff_s=retry_backoff_s,
    )
