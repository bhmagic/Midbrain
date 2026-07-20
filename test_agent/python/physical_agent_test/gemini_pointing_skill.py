from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from google import genai
from google.genai import types

from .rgb_capture import RgbCapture


class PointingIdentificationSkill:
    """Finite Skill that captures one RGB frame and asks Gemini Robotics-ER."""

    def __init__(self, capture: RgbCapture, model: str):
        self.capture = capture
        self.model = model

    async def run(self, user_question: str) -> str:
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is empty in config/api_keys.env")
        captured = await self.capture.capture_latest()
        prompt = self._prompt(user_question)
        response_text = await asyncio.to_thread(
            self._call_gemini,
            api_key,
            captured.image_bytes,
            captured.mime_type,
            prompt,
        )
        result = {
            "answer": response_text,
            "screenshot": str(captured.path),
            "frame_id": captured.observation.get("frame_id"),
            "model": self.model,
            "input": "RGB only",
        }
        return json.dumps(result, ensure_ascii=False)

    def _call_gemini(
        self,
        api_key: str,
        image_bytes: bytes,
        mime_type: str,
        prompt: str,
    ) -> str:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.2,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = response.text
        if not text:
            raise RuntimeError("Gemini returned no text")
        return text.strip()

    @staticmethod
    def _prompt(user_question: str) -> str:
        return f"""
Use only the supplied single RGB image. Do not assume depth, IMU, previous frames,
or hidden sensor data. A person may be pointing at an object in the scene.

User request: {user_question}

Identify the most likely object being pointed at. If the pointing gesture is not
visible or is ambiguous, say so directly and name up to two plausible objects.
Return a concise answer with:
1. object label,
2. brief visual reason,
3. confidence as low, medium, or high.
Do not claim that the robot has moved or interacted with anything.
""".strip()
