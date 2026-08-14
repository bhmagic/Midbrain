from __future__ import annotations

import math
import unittest

import numpy as np

from slicing_skill import (
    ABSOLUTE_WORLD_POINT_MODE,
    BLADE_PROFILE_EXTENSION_ID,
    RELATIVE_WORLD_POINT_MODE,
    blade_profiles_from_effector,
    build_slicing_plan,
    motion_profiles_from_document,
    resolve_point_arguments,
    select_active_workcell_activation,
)


def activation(
    *,
    activation_id: str = "activation-1",
    translation=(0.0, 0.0, 0.0),
    rotation=(0.0, 0.0, 0.0, 1.0),
):
    return {
        "activation_id": activation_id,
        "calibration_revision": "calibration-1",
        "world_frame": "world",
        "arm_base_frame": "arm_base",
        "state": "ACTIVE",
        "motion_usable": True,
        "expires_at": None,
        "expires_at_us": None,
        "transforms": {
            "world_from_base": {
                "translation_m": list(translation),
                "rotation_xyzw": list(rotation),
            }
        },
    }


def effector_profile():
    return {
        "schema": "midbrain.mounted_effector_profile",
        "schema_version": 1,
        "profile_id": "test.blade",
        "profile_revision": "test-1",
        "extensions": {
            BLADE_PROFILE_EXTENSION_ID: {
                "schema": "midbrain.effector_slicing_blade_profiles",
                "schema_version": 1,
                "default_profile_number": 1,
                "profiles": [
                    {
                        "profile_number": 1,
                        "name": "Default blade use",
                        "blade_direction_effector": [1.0, 0.0, 0.0],
                        "slicing_direction_effector": [0.2, 1.0, 0.0],
                        "locked_joint_names": ["joint6"],
                    },
                    {
                        "profile_number": 2,
                        "name": "Alternate blade use",
                        "blade_direction_effector": [0.0, 0.0, -1.0],
                        "slicing_direction_effector": [-1.0, 0.0, 0.0],
                        "locked_joint_names": [],
                    },
                ],
            }
        },
    }


def motion_profiles():
    return {
        "schema": "midbrain.slicing_motion_profiles",
        "schema_version": 1,
        "default_profile_number": 1,
        "profiles": [
            {
                "profile_number": 1,
                "name": "Default motion",
                "blade_load_kgf": 2.0,
                "retract_distance_m": 0.2,
                "delay_after_engage_s": 0.4,
                "slice_wait_speed_m_s": 2.0,
                "delay_after_retract_s": 0.7,
            },
            {
                "profile_number": 2,
                "name": "Slow motion",
                "blade_load_kgf": 1.5,
                "retract_distance_m": 0.1,
                "delay_after_engage_s": 0.6,
                "slice_wait_speed_m_s": 0.5,
                "delay_after_retract_s": 0.8,
            },
        ],
    }


def arguments(*, explicit_profiles: bool = True):
    result = {
        "blade_profile_number": 1,
        "motion_profile_number": 1,
        "slice_begin_point_m": [1.0, 2.0, 3.0],
        "blade_direction_world": [0.0, 0.0, -1.0],
        "slicing_direction_world": [1.0, 0.0, 0.3],
        "slice_length_m": 1.0,
    }
    if explicit_profiles:
        result.update(
            {
                "blade_direction_effector": [1.0, 0.0, 0.0],
                "slicing_direction_effector": [0.2, 1.0, 0.0],
                "locked_joint_names": ["joint6"],
                "blade_load_kgf": 2.0,
                "retract_distance_m": 0.2,
                "delay_after_engage_s": 0.4,
                "slice_wait_speed_m_s": 2.0,
                "delay_after_retract_s": 0.7,
            }
        )
    return result


def rotation_from_quaternion(values):
    x, y, z, w = np.asarray(values, dtype=float) / np.linalg.norm(values)
    return np.asarray(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


class SlicingPlanTests(unittest.TestCase):
    def test_absolute_point_entry_preserves_begin_point(self):
        data = arguments()
        resolved, metadata = resolve_point_arguments(
            data,
            activation(),
            point_mode=ABSOLUTE_WORLD_POINT_MODE,
        )
        self.assertEqual(resolved["slice_begin_point_m"], [1.0, 2.0, 3.0])
        self.assertIsNone(metadata["captured_current_effector_world_m"])

    def test_relative_begin_point_uses_one_measured_world_origin(self):
        data = arguments()
        data["slice_begin_point_m"] = [0.1, 0.0, -0.2]
        resolved, metadata = resolve_point_arguments(
            data,
            activation(translation=(1.0, 2.0, 3.0)),
            point_mode=RELATIVE_WORLD_POINT_MODE,
            current_effector_arm_base_m=[0.4, 0.5, 0.6],
        )
        np.testing.assert_allclose(
            metadata["captured_current_effector_world_m"],
            [1.4, 2.5, 3.6],
        )
        np.testing.assert_allclose(
            resolved["slice_begin_point_m"],
            [1.5, 2.5, 3.4],
        )

    def test_blade_priority_and_slicing_projection_define_orientation(self):
        plan = build_slicing_plan(arguments(), activation())
        rotation = rotation_from_quaternion(plan.contact_steps[0].orientation_xyzw)
        np.testing.assert_allclose(
            rotation @ np.asarray([1.0, 0.0, 0.0]),
            [0.0, 0.0, -1.0],
            atol=1e-7,
        )
        np.testing.assert_allclose(
            rotation @ np.asarray([0.0, 1.0, 0.0]),
            [1.0, 0.0, 0.0],
            atol=1e-7,
        )
        self.assertEqual(
            plan.contact_steps[0].locked_joint_names,
            ("joint6",),
        )

    def test_integrated_alignment_backend_defaults_and_is_selectable(self):
        default_plan = build_slicing_plan(arguments(), activation())
        pos_speed_arguments = arguments()
        pos_speed_arguments["integrated_execution_backend"] = "pos_speed"
        pos_speed_plan = build_slicing_plan(
            pos_speed_arguments,
            activation(),
        )

        self.assertEqual(
            default_plan.integrated_alignment_arguments["execution_backend"],
            "IMPEDANCE",
        )
        self.assertEqual(
            pos_speed_plan.integrated_alignment_arguments["execution_backend"],
            "POS_SPEED",
        )
        with self.assertRaisesRegex(
            ValueError,
            "integrated_execution_backend must be IMPEDANCE or POS_SPEED",
        ):
            invalid = arguments()
            invalid["integrated_execution_backend"] = "POS_TOR"
            build_slicing_plan(invalid, activation())

    def test_physical_trial_vectors_map_slice_to_world_negative_y(self):
        data = arguments()
        data.update(
            {
                "blade_direction_effector": [0.5, -0.1, -1.0],
                "slicing_direction_effector": [-1.0, 0.0, -0.5],
                "blade_direction_world": [0.0, 0.0, -1.0],
                "slicing_direction_world": [0.0, -1.0, 0.0],
            }
        )
        plan = build_slicing_plan(data, activation())
        rotation = rotation_from_quaternion(plan.contact_steps[0].orientation_xyzw)
        blade = np.asarray(data["blade_direction_effector"], dtype=float)
        blade /= np.linalg.norm(blade)
        slicing = np.asarray(data["slicing_direction_effector"], dtype=float)
        slicing -= np.dot(slicing, blade) * blade
        slicing /= np.linalg.norm(slicing)
        np.testing.assert_allclose(rotation @ blade, [0.0, 0.0, -1.0], atol=1e-7)
        np.testing.assert_allclose(rotation @ slicing, [0.0, -1.0, 0.0], atol=1e-7)
        np.testing.assert_allclose(
            plan.contact_steps[1].position_m,
            [0.0, -1.0, 0.0],
            atol=1e-7,
        )

    def test_path_is_begin_slice_then_negative_blade_retract(self):
        plan = build_slicing_plan(arguments(), activation())
        np.testing.assert_allclose(plan.contact_steps[0].position_m, [1, 2, 3])
        np.testing.assert_allclose(plan.contact_steps[1].position_m, [1, 0, 0])
        np.testing.assert_allclose(plan.contact_steps[2].position_m, [0, 0, 0.2])
        self.assertEqual(
            [step.position_mode for step in plan.contact_steps],
            [
                "ABSOLUTE_ROOT",
                "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
                "RELATIVE_TO_MEASURED_EFFECTOR_ROOT_AXES",
            ],
        )
        np.testing.assert_allclose(
            plan.path["slice_delta_arm_base_m"],
            [1, 0, 0],
        )
        np.testing.assert_allclose(
            plan.path["planned_retract_endpoint_world_m"],
            [2, 2, 3.2],
        )
        self.assertEqual(
            plan.path["construction"],
            "ABSOLUTE_BEGIN_THEN_MEASURED_START_PROJECTED_SLICE_"
            "THEN_MEASURED_START_NEGATIVE_BLADE_RETRACT",
        )
        self.assertEqual(
            [step.motion_type for step in plan.contact_steps],
            ["CARTESIAN_SEGMENT"] * 3,
        )

    def test_three_steps_share_force_and_use_length_based_slice_wait(self):
        plan = build_slicing_plan(arguments(), activation())
        load_n = 2.0 * 9.80665
        expected_force = [0.5 * load_n, -0.5 * load_n, -load_n]
        for step in plan.contact_steps:
            np.testing.assert_allclose(step.force_n, expected_force, atol=1e-8)
            self.assertEqual(step.torque_nm, (0.0, 0.0, 0.0))
        self.assertEqual(
            [step.delay_after_accept_s for step in plan.contact_steps],
            [0.4, 0.5, 0.7],
        )
        self.assertIn("NOT_CARTESIAN_SPEED_CONTROL", plan.timing["slice_wait_semantics"])

    def test_world_points_and_orientation_convert_to_arm_base(self):
        half = math.sqrt(0.5)
        plan = build_slicing_plan(
            arguments(),
            activation(
                translation=(1.0, 2.0, 0.0),
                rotation=(0.0, 0.0, half, half),
            ),
        )
        np.testing.assert_allclose(plan.contact_steps[0].position_m, [0, 0, 3])
        np.testing.assert_allclose(
            plan.contact_steps[1].position_m,
            [0, -1, 0],
            atol=1e-12,
        )

    def test_agent_defaults_resolve_profile_number_one(self):
        data = arguments(explicit_profiles=False)
        data["blade_profile_number"] = None
        data["motion_profile_number"] = None
        plan = build_slicing_plan(
            data,
            activation(),
            effector_profile=effector_profile(),
            motion_profiles_document=motion_profiles(),
        )
        self.assertEqual(plan.blade_profile_number, 1)
        self.assertEqual(plan.motion_profile_number, 1)
        self.assertEqual(plan.alignment["blade_profile_source"], "ACTIVE_EFFECTOR_PROFILE")
        self.assertEqual(plan.timing["motion_profile_source"], "SLICING_SKILL_CONFIG")

    def test_null_profile_selectors_resolve_current_non_one_defaults(self):
        blade = effector_profile()
        blade["extensions"][BLADE_PROFILE_EXTENSION_ID][
            "default_profile_number"
        ] = 2
        motion = motion_profiles()
        motion["default_profile_number"] = 2
        data = arguments(explicit_profiles=False)
        data["blade_profile_number"] = None
        data["motion_profile_number"] = None

        plan = build_slicing_plan(
            data,
            activation(),
            effector_profile=blade,
            motion_profiles_document=motion,
        )

        self.assertEqual(plan.blade_profile_number, 2)
        self.assertEqual(plan.motion_profile_number, 2)
        self.assertEqual(plan.load["blade_load_kgf"], 1.5)
        self.assertEqual(plan.alignment["blade_profile_selection"], "LIVE_DEFAULT")
        self.assertEqual(plan.timing["motion_profile_selection"], "LIVE_DEFAULT")

    def test_numbered_profile_two_is_selectable(self):
        data = arguments(explicit_profiles=False)
        data["blade_profile_number"] = 2
        data["motion_profile_number"] = 2
        plan = build_slicing_plan(
            data,
            activation(),
            effector_profile=effector_profile(),
            motion_profiles_document=motion_profiles(),
        )
        self.assertEqual(plan.blade_profile_number, 2)
        self.assertEqual(plan.motion_profile_number, 2)
        self.assertEqual(plan.load["blade_load_kgf"], 1.5)

    def test_profile_documents_accept_any_existing_default(self):
        blade = effector_profile()
        blade["extensions"][BLADE_PROFILE_EXTENSION_ID]["default_profile_number"] = 2
        self.assertEqual(blade_profiles_from_effector(blade)["default_profile_number"], 2)
        motion = motion_profiles()
        motion["profiles"] = motion["profiles"][1:]
        motion["default_profile_number"] = 2
        self.assertEqual(
            motion_profiles_from_document(motion)["default_profile_number"],
            2,
        )

    def test_rejects_degenerate_local_or_world_projection(self):
        local = arguments()
        local["slicing_direction_effector"] = [2.0, 0.0, 0.0]
        with self.assertRaisesRegex(ValueError, "parallel"):
            build_slicing_plan(local, activation())
        world = arguments()
        world["slicing_direction_world"] = [0.0, 0.0, 2.0]
        with self.assertRaisesRegex(ValueError, "parallel"):
            build_slicing_plan(world, activation())

    def test_rejects_slice_wait_longer_than_contact_window(self):
        data = arguments()
        data["slice_wait_speed_m_s"] = 0.001
        with self.assertRaisesRegex(ValueError, "must be in"):
            build_slicing_plan(data, activation())

    def test_selects_exactly_one_motion_usable_nonexpiring_activation(self):
        selected = select_active_workcell_activation({"activations": [activation()]})
        self.assertEqual(selected["activation_id"], "activation-1")
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            select_active_workcell_activation(
                {"activations": [activation(), activation(activation_id="activation-2")]}
            )


if __name__ == "__main__":
    unittest.main()
