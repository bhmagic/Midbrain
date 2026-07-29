from __future__ import annotations

import time
import unittest

from physical_agent_test.vlm_router import VisionLanguageRouter


class _Backend:
    def __init__(
        self,
        backend_id: str,
        model_id: str,
        *,
        result: str | None = None,
        error: str | None = None,
    ):
        self.backend_id = backend_id
        self.model_id = model_id
        self.result = result
        self.error = error
        self.call_count = 0

    def generate(self, image_bytes: bytes, mime_type: str, prompt: str) -> str:
        self.call_count += 1
        if self.error:
            raise RuntimeError(self.error)
        return str(self.result)


class VisionLanguageRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_falls_back_and_records_backend_provenance(self) -> None:
        local = _Backend("local.vlm", "small", error="low confidence")
        remote = _Backend("remote.vlm", "large", result="visible object")
        router = VisionLanguageRouter([local, remote])

        result = await router.generate(
            image_bytes=b"image",
            mime_type="image/jpeg",
            prompt="What is visible?",
        )

        self.assertEqual(result.text, "visible object")
        self.assertEqual(result.backend_id, "remote.vlm")
        self.assertEqual(result.model_id, "large")
        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(result.failed_attempts[0]["backend_id"], "local.vlm")
        self.assertEqual(local.call_count, 1)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(result.input_bytes, 5)
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertEqual(len(result.input_sha256), 64)

    def test_voting_and_qc_remain_disabled_for_future_work(self) -> None:
        backend = _Backend("test", "model", result="ok")

        with self.assertRaisesRegex(ValueError, "not enabled"):
            VisionLanguageRouter(
                [backend],
                quality_control_mode="MAJORITY_VOTE",
            )

    async def test_each_backend_attempt_has_a_hard_timeout(self) -> None:
        class _StuckBackend(_Backend):
            def generate(
                self,
                image_bytes: bytes,
                mime_type: str,
                prompt: str,
            ) -> str:
                time.sleep(0.2)
                return "late"

        fallback = _Backend("fallback", "fast", result="usable")
        router = VisionLanguageRouter(
            [_StuckBackend("stuck", "slow"), fallback],
            attempt_timeout_s=0.02,
        )

        result = await router.generate(
            image_bytes=b"image",
            mime_type="image/jpeg",
            prompt="Inspect",
        )

        self.assertEqual(result.text, "usable")
        self.assertIn("timeout after", result.failed_attempts[0]["error"])


if __name__ == "__main__":
    unittest.main()
