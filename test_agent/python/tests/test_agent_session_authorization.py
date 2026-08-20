from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import Agent, RunState
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_internal.agent_runner_helpers import resolve_resumed_context
from fastapi import HTTPException
from pydantic import ValidationError

os.environ.setdefault("OPENAI_API_KEY", "unit-test-placeholder")

from physical_agent_test.app import (
    DeveloperApprovalDecision,
    DEFAULT_SESSION_AUTO_SPEED_M_S,
    PromptRequest,
    _approval_fingerprint,
    _record_approval_decisions,
    _repeated_approval_response,
    _session_authorization,
    _validate_automatic_agent_approval,
)
from physical_agent_test.agent_driver import (
    AgentRunContext,
    AgentSessionAuthorization,
    PrototypeAgentDriver,
    _runner_context,
    provider_activation_needs_approval,
    relative_motion_needs_approval,
    safe_home_needs_approval,
    space_reinitialization_needs_approval,
    arm_base_activation_needs_approval,
)


def _interruption(tool_name: str, arguments: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_name=tool_name,
        tool_namespace=None,
        raw_item={"arguments": arguments},
    )


def _pose_motion_arguments(**overrides) -> dict[str, object]:
    arguments: dict[str, object] = {
        "preview_id": "preview-pose",
        "motion_intent": "NEW_RELATIVE_MOVE",
        "direction": "ARM_BASE_POSITIVE_X",
        "distance_m": 0.2,
        "requested_speed_m_s": 0.2,
        "planned_nominal_speed_m_s": 0.2,
        "orientation_policy": "POSITION_ONLY",
        "controlled_frame_yaw_delta_deg": None,
    }
    arguments.update(overrides)
    return arguments


class AgentSessionAuthorizationTests(unittest.TestCase):
    def test_resumed_run_keeps_sdk_context_and_approval_records(self) -> None:
        authorization = AgentSessionAuthorization(
            auto_authorize_provider_activation=True,
        )
        agent = Agent(name="resume-test", instructions="test")
        state = RunState(
            RunContextWrapper(authorization),
            "activate camera",
            agent,
        )
        approval = ToolApprovalItem(
            agent=agent,
            raw_item={
                "type": "function_call",
                "call_id": "camera-hot-call",
                "name": "set_provider_residency",
                "arguments": (
                    '{"provider_id":"camera.femto_bolt","action":"hot"}'
                ),
            },
            tool_name="set_provider_residency",
        )
        state.approve(approval)

        selected = _runner_context(
            state,
            AgentSessionAuthorization(),
        )
        resolved = resolve_resumed_context(
            run_state=state,
            context=selected,
        )

        self.assertIsNone(selected)
        self.assertIs(resolved.context, authorization)
        self.assertTrue(
            resolved.get_approval_status(
                "set_provider_residency",
                "camera-hot-call",
                existing_pending=approval,
            )
        )

    def test_fresh_run_receives_explicit_or_default_authorization(self) -> None:
        explicit = AgentSessionAuthorization(
            auto_authorize_provider_activation=True,
        )

        explicit_context = _runner_context("prompt", explicit)
        self.assertIsInstance(explicit_context, AgentRunContext)
        assert explicit_context is not None
        self.assertIs(explicit_context.authorization, explicit)
        self.assertEqual(explicit_context.loaded_tool_names, set())
        default_context = _runner_context("prompt", None)
        self.assertIsInstance(default_context, AgentRunContext)
        assert default_context is not None
        self.assertEqual(
            default_context.authorization,
            AgentSessionAuthorization(),
        )

    def test_repeated_exact_approval_is_stopped_without_sdk_call_ids(
        self,
    ) -> None:
        first = _interruption(
            "set_provider_residency",
            '{"provider_id":"camera.femto_bolt","action":"hot"}',
        )
        first.raw_item["call_id"] = "call-1"
        second = _interruption(
            "set_provider_residency",
            '{"action":"hot","provider_id":"camera.femto_bolt"}',
        )
        second.raw_item["call_id"] = "call-2"
        decisions = _record_approval_decisions(
            [first],
            approved=True,
            existing={},
        )
        approval = PrototypeAgentDriver._approval_description(second)

        self.assertIn(_approval_fingerprint(approval), decisions)
        response = _repeated_approval_response(
            run_id="run-1",
            approvals=[approval],
            decisions=decisions,
        )
        self.assertIsNotNone(response)
        assert response is not None
        self.assertTrue(response["approval_loop_prevented"])
        self.assertEqual(response["status"], "completed")
        self.assertIn("previously approved", response["answer"])

    def test_prompt_authorization_accepts_operator_limit_above_controller_limit(
        self,
    ) -> None:
        request = PromptRequest(
            prompt="move",
            max_auto_move_cm=35.0,
            max_auto_speed_m_s=0.5,
        )

        self.assertEqual(request.max_auto_move_cm, 35.0)
        self.assertEqual(request.max_auto_speed_m_s, 0.5)
        self.assertEqual(DEFAULT_SESSION_AUTO_SPEED_M_S, 5.0)
        self.assertEqual(
            PromptRequest(prompt="move", max_auto_speed_m_s=50.0).max_auto_speed_m_s,
            50.0,
        )

    def test_provider_auto_authorization_excludes_stop(self) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_PROVIDER_ACTIVATION",
        )
        _validate_automatic_agent_approval(
            [
                _interruption(
                    "set_provider_residency",
                    '{"provider_id":"camera.femto_bolt","action":"hot"}',
                )
            ],
            decision,
        )

        with self.assertRaisesRegex(HTTPException, "permits only start"):
            _validate_automatic_agent_approval(
                [
                    _interruption(
                        "set_provider_residency",
                        (
                            '{"provider_id":"camera.femto_bolt",'
                            '"action":"stop"}'
                        ),
                    )
                ],
                decision,
            )

    def test_provider_stop_has_a_separate_auto_authorization(self) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_PROVIDER_STOP",
        )
        _validate_automatic_agent_approval(
            [
                _interruption(
                    "set_provider_residency",
                    '{"provider_id":"camera.femto_bolt","action":"stop"}',
                )
            ],
            decision,
        )

        with self.assertRaisesRegex(HTTPException, "permits only exact stop"):
            _validate_automatic_agent_approval(
                [
                    _interruption(
                        "set_provider_residency",
                        (
                            '{"provider_id":"camera.femto_bolt",'
                            '"action":"hot"}'
                        ),
                    )
                ],
                decision,
            )

    def test_recovery_authorizations_are_exact_tool_only(self) -> None:
        safe_home = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_SAFE_HOME",
        )
        reinitialization = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_SPACE_REINITIALIZATION",
        )

        _validate_automatic_agent_approval(
            [_interruption("execute_basic_safe_home", "{}")],
            safe_home,
        )
        _validate_automatic_agent_approval(
            [
                _interruption(
                    "reinitialize_space_cognition",
                    '{"reason":"recover spatial drift"}',
                )
            ],
            reinitialization,
        )
        with self.assertRaises(HTTPException):
            _validate_automatic_agent_approval(
                [_interruption("execute_basic_safe_home", "{}")],
                reinitialization,
            )

    def test_motion_auto_authorization_enforces_exact_tool_and_cm_limit(
        self,
    ) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_BOUNDED_RELATIVE_MOTION",
            max_auto_move_cm=10.0,
            max_auto_speed_m_s=0.2,
        )
        _validate_automatic_agent_approval(
            [
                _interruption(
                    "perform_relative_effector_motion",
                    '{"preview_id":"preview-1","distance_m":0.1,'
                    '"motion_intent":"NEW_RELATIVE_MOVE",'
                    '"direction":"POSITIVE_X",'
                    '"requested_speed_m_s":0.2,'
                    '"planned_nominal_speed_m_s":0.2,'
                    '"orientation_policy":"POSITION_ONLY",'
                    '"controlled_frame_yaw_delta_deg":null}',
                )
            ],
            decision,
        )

        with self.assertRaisesRegex(HTTPException, "10 cm limit"):
            _validate_automatic_agent_approval(
                [
                    _interruption(
                        "perform_relative_effector_motion",
                        '{"preview_id":"preview-2","distance_m":0.1001,'
                        '"motion_intent":"NEW_RELATIVE_MOVE",'
                        '"direction":"POSITIVE_X",'
                        '"requested_speed_m_s":0.2,'
                        '"planned_nominal_speed_m_s":0.2,'
                        '"orientation_policy":"POSITION_ONLY",'
                        '"controlled_frame_yaw_delta_deg":null}',
                    )
                ],
                decision,
            )
        with self.assertRaisesRegex(HTTPException, "exact Integrated"):
            _validate_automatic_agent_approval(
                [_interruption("execute_basic_safe_home", "{}")],
                decision,
            )

    def test_motion_resume_authorization_uses_host_canonical_envelope(
        self,
    ) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_BOUNDED_RELATIVE_MOTION",
            max_auto_move_cm=25.0,
            max_auto_speed_m_s=0.3,
        )
        interruption = _interruption(
            "perform_relative_effector_motion",
            '{"preview_id":"preview-opaque"}',
        )
        approval = PrototypeAgentDriver._approval_description(
            interruption,
            canonical_motion_arguments=_pose_motion_arguments(
                preview_id="preview-opaque",
            ),
        )

        _validate_automatic_agent_approval([approval], decision)

        self.assertEqual(
            approval["request"]["arguments"],
            '{"preview_id":"preview-opaque"}',
        )
        self.assertEqual(
            approval["authorization_arguments"]["distance_m"],
            0.2,
        )

    def test_prepared_motion_uses_the_same_bounded_authorization(self) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_BOUNDED_RELATIVE_MOTION",
            max_auto_move_cm=25.0,
            max_auto_speed_m_s=0.3,
        )
        interruption = _interruption(
            "perform_relative_effector_motion",
            '{"direction":"UP","distance_m":0.2}',
        )
        approval = PrototypeAgentDriver._approval_description(
            interruption,
            canonical_motion_arguments=_pose_motion_arguments(
                preview_id="preview-prepared",
            ),
        )

        _validate_automatic_agent_approval([approval], decision)

        self.assertEqual(
            approval["authorization_arguments"]["preview_id"],
            "preview-prepared",
        )

    def test_motion_approval_fingerprint_ignores_ephemeral_preview_id(
        self,
    ) -> None:
        first = PrototypeAgentDriver._approval_description(
            _interruption(
                "perform_relative_effector_motion",
                '{"direction":"UP","distance_m":0.2}',
            ),
            canonical_motion_arguments=_pose_motion_arguments(
                preview_id="preview-1",
            ),
        )
        repeated = PrototypeAgentDriver._approval_description(
            _interruption(
                "perform_relative_effector_motion",
                '{"preview_id":"preview-2"}',
            ),
            canonical_motion_arguments=_pose_motion_arguments(
                preview_id="preview-2",
            ),
        )
        new_target = PrototypeAgentDriver._approval_description(
            _interruption(
                "perform_relative_effector_motion",
                '{"direction":"UP","distance_m":0.2}',
            ),
            canonical_motion_arguments=_pose_motion_arguments(
                preview_id="preview-3",
                target_position_m=[0.1, 0.2, 0.7],
            ),
        )

        self.assertEqual(
            _approval_fingerprint(first),
            _approval_fingerprint(repeated),
        )
        self.assertNotEqual(
            _approval_fingerprint(first),
            _approval_fingerprint(new_target),
        )

    def test_motion_auto_authorization_covers_bounded_pose_yaw_only(
        self,
    ) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_BOUNDED_RELATIVE_MOTION",
            max_auto_move_cm=35.0,
            max_auto_speed_m_s=0.5,
        )
        pure_rotation = _pose_motion_arguments(
            motion_intent="NEW_RELATIVE_ROTATION",
            direction="NONE",
            distance_m=0.0,
            requested_speed_m_s=None,
            planned_nominal_speed_m_s=0.0,
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )
        combined_pose = _pose_motion_arguments(
            motion_intent="NEW_RELATIVE_POSE_MOVE",
            orientation_policy="APPLY_CONTROLLED_FRAME_YAW_DELTA",
            controlled_frame_yaw_delta_deg=-30.0,
        )
        for arguments in (pure_rotation, combined_pose):
            with self.subTest(arguments=arguments):
                _validate_automatic_agent_approval(
                    [
                        _interruption(
                            "perform_relative_effector_motion",
                            json.dumps(arguments),
                        )
                    ],
                    decision,
                )

        invalid = [
            {**pure_rotation, "controlled_frame_yaw_delta_deg": -45.001},
            {**pure_rotation, "motion_intent": "NEW_RELATIVE_MOVE"},
            {
                **pure_rotation,
                "requested_speed_m_s": 0.1,
                "planned_nominal_speed_m_s": 0.1,
            },
            {**pure_rotation, "distance_m": 0.1},
        ]
        for arguments in invalid:
            with self.subTest(invalid=arguments):
                with self.assertRaises(HTTPException):
                    _validate_automatic_agent_approval(
                        [
                            _interruption(
                                "perform_relative_effector_motion",
                                json.dumps(arguments),
                            )
                        ],
                        decision,
                    )

    def test_motion_auto_authorization_uses_joint_speed_policy(
        self,
    ) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_BOUNDED_RELATIVE_MOTION",
            max_auto_move_cm=35.0,
            max_auto_speed_m_s=0.5,
        )
        _validate_automatic_agent_approval(
            [
                _interruption(
                    "perform_relative_effector_motion",
                    '{"preview_id":"preview-1",'
                    '"motion_intent":"NEW_RELATIVE_MOVE",'
                    '"direction":"POSITIVE_X","distance_m":0.2,'
                    '"requested_speed_m_s":0.5,'
                    '"planned_nominal_speed_m_s":0.5,'
                    '"orientation_policy":"POSITION_ONLY",'
                    '"controlled_frame_yaw_delta_deg":null}',
                )
            ],
            decision,
        )

        with self.assertRaisesRegex(HTTPException, "10/20 rad/s"):
            _validate_automatic_agent_approval(
                [
                    _interruption(
                        "perform_relative_effector_motion",
                        '{"preview_id":"preview-2",'
                        '"motion_intent":"NEW_RELATIVE_MOVE",'
                        '"direction":"POSITIVE_X","distance_m":0.2,'
                        '"requested_speed_m_s":0.5001,'
                        '"planned_nominal_speed_m_s":0.5001,'
                        '"requested_peak_joint_speed_rad_s":10.1,'
                        '"joint_speed_authentication_required":true,'
                        '"orientation_policy":"POSITION_ONLY",'
                        '"controlled_frame_yaw_delta_deg":null}',
                    )
                ],
                decision,
            )

    def test_calibration_activation_auto_authorization_is_exact_tool_only(
        self,
    ) -> None:
        decision = DeveloperApprovalDecision(
            approve=True,
            approval_mode="AUTO_ARM_BASE_ACTIVATION",
        )
        _validate_automatic_agent_approval(
            [
                _interruption(
                    "review_and_activate_arm_base",
                    (
                        '{"candidate_id":"candidate-1",'
                        '"candidate_sha256":"'
                        + "a" * 64
                        + '"}'
                    ),
                )
            ],
            decision,
        )

        with self.assertRaisesRegex(
            HTTPException,
            "permits only exact candidate",
        ):
            _validate_automatic_agent_approval(
                [_interruption("execute_basic_safe_home", "{}")],
                decision,
            )


class DynamicAgentApprovalPredicateTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_eligible_calls_do_not_require_sdk_approval(self) -> None:
        context = SimpleNamespace(
            context=AgentSessionAuthorization(
                auto_authorize_provider_activation=True,
                auto_authorize_provider_stop=True,
                auto_authorize_relative_motion=True,
                max_auto_move_cm=35.0,
                max_auto_speed_m_s=0.2,
                auto_authorize_arm_base_activation=True,
                auto_authorize_safe_home=True,
                auto_authorize_space_reinitialization=True,
            )
        )

        self.assertFalse(
            await provider_activation_needs_approval(
                context,
                {"provider_id": "robot_arm.primary.integrated", "action": "hot"},
                "provider-call",
            )
        )
        self.assertFalse(
            await provider_activation_needs_approval(
                context,
                {"provider_id": "camera.femto_bolt", "action": "stop"},
                "provider-stop-call",
            )
        )
        self.assertFalse(
            await relative_motion_needs_approval(
                context,
                {
                    "motion_intent": "NEW_RELATIVE_MOVE",
                    "direction": "POSITIVE_X",
                    "distance_m": 0.2,
                    "requested_speed_m_s": 0.2,
                    "planned_nominal_speed_m_s": 0.2,
                    "orientation_policy": "POSITION_ONLY",
                    "controlled_frame_yaw_delta_deg": None,
                },
                "motion-call",
            )
        )
        self.assertFalse(
            await relative_motion_needs_approval(
                context,
                {
                    "motion_intent": "NEW_RELATIVE_MOVE",
                    "direction": "TARGET_VECTOR",
                    "distance_m": 0.05,
                    "planned_nominal_speed_m_s": 0.03,
                    "orientation_policy": "PRESERVE_CURRENT",
                    "controlled_frame_yaw_delta_deg": None,
                },
                "no-contact-motion-call",
            )
        )
        self.assertFalse(
            await arm_base_activation_needs_approval(
                context,
                {
                    "alignment_id": "alignment-1",
                    "candidate_sha256": "a" * 64,
                },
                "activation-call",
            )
        )
        self.assertFalse(
            await safe_home_needs_approval(context, {}, "safe-home-call")
        )
        self.assertFalse(
            await space_reinitialization_needs_approval(
                context,
                {"reason": "recover spatial drift"},
                "reinitialization-call",
            )
        )

    async def test_session_authorization_remains_fail_closed(self) -> None:
        context = SimpleNamespace(
            context=AgentSessionAuthorization(
                auto_authorize_provider_activation=True,
                auto_authorize_relative_motion=True,
                max_auto_move_cm=5.0,
                max_auto_speed_m_s=0.1,
                auto_authorize_arm_base_activation=False,
                auto_authorize_safe_home=False,
                auto_authorize_space_reinitialization=False,
            )
        )

        self.assertTrue(
            await provider_activation_needs_approval(
                context,
                {"provider_id": "camera.femto_bolt", "action": "stop"},
                "stop-call",
            )
        )
        self.assertTrue(
            await relative_motion_needs_approval(
                context,
                {
                    "distance_m": 0.0501,
                    "planned_nominal_speed_m_s": 0.1,
                },
                "motion-call",
            )
        )
        self.assertTrue(
            await relative_motion_needs_approval(
                context,
                {
                    "distance_m": 0.05,
                    "planned_nominal_speed_m_s": 0.1001,
                },
                "fast-motion-call",
            )
        )
        self.assertTrue(
            await arm_base_activation_needs_approval(
                context,
                {},
                "activation-call",
            )
        )
        self.assertTrue(
            await safe_home_needs_approval(context, {}, "safe-home-call")
        )
        self.assertTrue(
            await space_reinitialization_needs_approval(
                context,
                {"reason": "recover spatial drift"},
                "reinitialization-call",
            )
        )

    async def test_motion_predicate_uses_joint_speed_authentication_threshold(
        self,
    ) -> None:
        context = SimpleNamespace(
            context=AgentSessionAuthorization(
                auto_authorize_relative_motion=True,
                max_auto_move_cm=35.0,
                max_auto_speed_m_s=0.5,
            )
        )

        self.assertFalse(
            await relative_motion_needs_approval(
                context,
                {
                    "motion_intent": "NEW_RELATIVE_MOVE",
                    "direction": "POSITIVE_X",
                    "distance_m": 0.2,
                    "requested_speed_m_s": 0.5,
                    "planned_nominal_speed_m_s": 0.5,
                    "orientation_policy": "POSITION_ONLY",
                    "controlled_frame_yaw_delta_deg": None,
                },
                "motion-call",
            )
        )
        self.assertTrue(
            await relative_motion_needs_approval(
                context,
                {
                    "motion_intent": "NEW_RELATIVE_MOVE",
                    "direction": "POSITIVE_X",
                    "distance_m": 0.2,
                    "requested_speed_m_s": 0.5001,
                    "planned_nominal_speed_m_s": 0.5001,
                    "requested_peak_joint_speed_rad_s": 10.1,
                    "joint_speed_authentication_required": True,
                    "orientation_policy": "POSITION_ONLY",
                    "controlled_frame_yaw_delta_deg": None,
                },
                "fast-motion-call",
            )
        )


class AutonomousRouteAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def test_streaming_route_builds_session_authorization(self) -> None:
        request = PromptRequest(
            prompt="establish the world and arm-base coordinates",
            auto_authorize_provider_activation=True,
            auto_authorize_provider_stop=True,
            auto_authorize_relative_motion=True,
            max_auto_move_cm=35.0,
            max_auto_speed_m_s=0.5,
            auto_authorize_arm_base_activation=True,
            auto_authorize_safe_home=True,
            auto_authorize_space_reinitialization=True,
        )
        authorization = _session_authorization(request)
        self.assertTrue(authorization.auto_authorize_provider_activation)
        self.assertTrue(authorization.auto_authorize_provider_stop)
        self.assertTrue(authorization.auto_authorize_relative_motion)
        self.assertEqual(authorization.max_auto_move_cm, 35.0)
        self.assertEqual(authorization.max_auto_speed_m_s, 0.5)
        self.assertTrue(authorization.auto_authorize_arm_base_activation)
        self.assertTrue(authorization.auto_authorize_safe_home)
        self.assertTrue(
            authorization.auto_authorize_space_reinitialization
        )


if __name__ == "__main__":
    unittest.main()
