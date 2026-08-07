from __future__ import annotations

import asyncio
import time
import unittest
from pathlib import Path

import pytest

pytest.importorskip("agents")

from physical_agent_test.agent_driver import PrototypeAgentDriver
from physical_agent_test.stationary_calibration_adapter import (
    FOUNDATIONPOSE_CANONICAL_INVOCATION,
    StationaryCalibrationSkillAdapter,
    mentions_foundation_pose,
)
from stationary_world_arm_alignment.math3d import YawUnobservableError


class _Runtime:
    def __init__(self):
        self.calls = []
        self.cancelled = False
        self.closed = False

    async def run(
        self,
        mode,
        *,
        arm_is_home=False,
        allow_active_control_interrupt=False,
    ):
        self.calls.append(
            (mode, arm_is_home, allow_active_control_interrupt)
        )
        return {"status": "CANDIDATE_REVIEW_REQUIRED"}

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True


class _PointingSkill:
    async def run(self, question: str) -> str:
        return question


class _ActivationService:
    def __init__(self):
        self.calls = []

    async def review_and_activate(self, **arguments):
        self.calls.append(arguments)
        return {"status": "ACTIVE", "motion_usable": True}


class StationaryCalibrationAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_is_deferred_and_does_not_claim_home_or_interrupt(
        self,
    ) -> None:
        created = []

        def factory():
            runtime = _Runtime()
            created.append(runtime)
            return runtime

        adapter = StationaryCalibrationSkillAdapter(factory)
        self.assertEqual(created, [])

        result = await adapter.run(request=FOUNDATIONPOSE_CANONICAL_INVOCATION)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].calls, [("auto", False, False)])
        self.assertFalse(
            result["agent_adapter"]["physical_motion_submitted_by_adapter"]
        )
        self.assertFalse(result["agent_adapter"]["arm_is_home_claimed"])
        self.assertFalse(
            result["agent_adapter"]["active_control_interrupt_allowed"]
        )
        self.assertTrue(created[0].closed)

    async def test_manifest_tool_requires_approval(self) -> None:
        adapter = StationaryCalibrationSkillAdapter(_Runtime)
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=Path(__file__).resolve().parents[3],
            eligible_tool_names={"calibrate_stationary_workcell"},
            stationary_calibration_skill=adapter,
            stationary_calibration_timeout_s=321.0,
        )
        tool = driver.agent.tools[0]

        self.assertEqual(tool.name, "calibrate_stationary_workcell")
        self.assertTrue(tool.needs_approval)
        self.assertEqual(tool.timeout_seconds, 321.0)

    async def test_candidate_requires_exact_activation_continuation(
        self,
    ) -> None:
        class _CandidateRuntime(_Runtime):
            async def run(self, *_args, **_kwargs):
                return {
                    "status": "CANDIDATE_REVIEW_REQUIRED",
                    "alignment_id": "alignment-1",
                    "candidate": {
                        "candidate_id": "alignment-1",
                        "expires_at_us": time.time_ns() // 1000
                        + 60_000_000,
                    },
                }

        activation = _ActivationService()
        adapter = StationaryCalibrationSkillAdapter(
            _CandidateRuntime,
            activation_service=activation,
        )

        candidate = await adapter.run(request=FOUNDATIONPOSE_CANONICAL_INVOCATION)
        next_tool = candidate["required_next_tool"]

        self.assertFalse(candidate["workflow_complete"])
        self.assertEqual(
            next_tool["name"],
            "review_and_activate_stationary_calibration",
        )
        activated = await adapter.review_and_activate(
            **next_tool["arguments"]
        )
        self.assertTrue(activated["motion_usable"])
        self.assertEqual(activation.calls, [next_tool["arguments"]])

    async def test_unobservable_yaw_returns_actionable_nonmotion_result(
        self,
    ) -> None:
        class _UnobservableRuntime(_Runtime):
            async def run(self, *_args, **_kwargs):
                raise YawUnobservableError(
                    {
                        "reason_code": (
                            "BASE_YAW_HORIZONTAL_LEVER_TOO_SMALL"
                        ),
                        "predicted_horizontal_lever_arm_m": 0.03,
                        "observed_horizontal_lever_arm_m": 0.02,
                        "minimum_horizontal_lever_arm_m": 0.1,
                    }
                )

        adapter = StationaryCalibrationSkillAdapter(_UnobservableRuntime)
        result = await adapter.run(request=FOUNDATIONPOSE_CANONICAL_INVOCATION)

        self.assertEqual(result["status"], "CALIBRATION_POSE_REQUIRED")
        self.assertEqual(result["reason_code"], "BASE_YAW_UNOBSERVABLE")
        self.assertFalse(result["motion_usable"])
        self.assertFalse(result["workflow_complete"])
        self.assertIn("Move the end effector sideways", result["message"])
        self.assertIn("minimum", result["required_operator_action"])
        self.assertTrue(result["agent_adapter"])

    async def test_regular_agent_always_exposes_world_arm_calibration(
        self,
    ) -> None:
        adapter = StationaryCalibrationSkillAdapter(_Runtime)
        driver = PrototypeAgentDriver(
            _PointingSkill(),
            "gpt-test",
            workspace_root=Path(__file__).resolve().parents[3],
            eligible_tool_names={"identify_pointed_object"},
            stationary_calibration_skill=adapter,
        )
        tools = {tool.name: tool for tool in driver.agent.tools}

        self.assertIn("calibrate_stationary_workcell", tools)
        self.assertIn(
            "review_and_activate_stationary_calibration",
            tools,
        )
        self.assertTrue(
            tools["calibrate_stationary_workcell"].needs_approval
        )
        self.assertIn(
            "world-to-arm-base",
            driver.agent.instructions,
        )
        self.assertIn(
            "Do not ask for conversational permission first",
            driver.agent.instructions,
        )

    async def test_request_without_foundationpose_name_does_not_load_it(self) -> None:
        created = []

        def factory():
            runtime = _Runtime()
            created.append(runtime)
            return runtime

        adapter = StationaryCalibrationSkillAdapter(factory)
        result = await adapter.run(request="establish both axes")

        self.assertEqual(created, [])
        self.assertEqual(
            result["status"],
            "FOUNDATIONPOSE_EXPLICIT_INVOCATION_REQUIRED",
        )
        self.assertEqual(
            result["required_name_mention"],
            "FoundationPose",
        )
        self.assertFalse(result["physical_motion_submitted"])

    async def test_foundationpose_name_variations_activate_runtime(self) -> None:
        requests = [
            FOUNDATIONPOSE_CANONICAL_INVOCATION,
            "please use foundation pose to align the arm base",
            "Run FOUNDATION-POSE calibration now",
            "use fundation pose for this stationary calibration",
            "start foundatoinpose alignment",
        ]
        for request in requests:
            with self.subTest(request=request):
                created = []

                def factory():
                    runtime = _Runtime()
                    created.append(runtime)
                    return runtime

                adapter = StationaryCalibrationSkillAdapter(factory)
                result = await adapter.run(request=request)

                self.assertTrue(mentions_foundation_pose(request))
                self.assertEqual(len(created), 1)
                self.assertTrue(
                    result["agent_adapter"]["foundationpose_name_match"]
                )

    def test_generic_alignment_words_do_not_match_foundationpose(self) -> None:
        for request in (
            "establish both axes",
            "calibrate the stationary arm base",
            "run pose alignment",
            "use the foundation alignment workflow",
        ):
            with self.subTest(request=request):
                self.assertFalse(mentions_foundation_pose(request))

    async def test_concurrent_run_is_rejected(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class _BlockingRuntime(_Runtime):
            async def run(self, *_args, **_kwargs):
                entered.set()
                await release.wait()
                return {"status": "DONE"}

        adapter = StationaryCalibrationSkillAdapter(_BlockingRuntime)
        first = asyncio.create_task(
            adapter.run(request=FOUNDATIONPOSE_CANONICAL_INVOCATION)
        )
        await entered.wait()
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                await adapter.run(request=FOUNDATIONPOSE_CANONICAL_INVOCATION)
        finally:
            release.set()
            await first


if __name__ == "__main__":
    unittest.main()
