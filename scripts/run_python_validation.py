from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable


WORKSPACE = Path(__file__).resolve().parents[1]

DEPENDENCY_SPECS = (
    "pip",
    "setuptools",
    "wheel",
    "build",
    "pytest>=8.3,<9",
    "numpy>=2.1,<3",
    "httpx>=0.27,<1",
    "Pillow>=10,<12",
    "opencv-python-headless>=4.10,<5",
    "fastapi>=0.115,<1",
    "uvicorn>=0.30,<1",
    "python-dotenv>=1,<2",
    "jsonschema>=4.23,<5",
    "openai-agents>=0.4,<1",
    "google-genai>=1.0,<2",
)

EDITABLE_PACKAGES = (
    "contracts/python",
    "providers/orbbec_femto_bolt/python",
    "providers/local_vio/python",
    "providers/arm_scene_compiler/python",
    "providers/sam2_scene_tracker/python",
    "providers/foundation_pose/python",
    "providers/rebot_arm_dm/python",
    "providers/rebot_arm_integrated/python",
    "providers/rebot_arm_contact/python",
    "providers/rebot_arm_grip/python",
    "skills/contact_work_runtime",
    "skills/grip_work_runtime",
    "skills/grip",
    "skills/slicing",
    "skills/grip-object",
    "skills/move-carried-object",
    "skills/let-go",
    "skills/lay-flat",
    "skills/limited-graph",
    "skills/spatial_registration_rgbd",
    "skills/locate-effector-front",
    "skills/observe_pointed_object",
    "skills/refine-arm-root-translation",
    "skills/register_tool_to_control_frame",
    "skills/locate_arm_base",
    "test_agent/python",
)

PYTHON_PATH_ENTRIES = (
    "contracts/python",
    "providers/local_vio",
    "providers/local_vio/python",
    "providers/orbbec_femto_bolt",
    "providers/orbbec_femto_bolt/python",
    "providers/arm_scene_compiler/python",
    "providers/sam2_scene_tracker/python",
    "providers/foundation_pose/python",
    "providers/rebot_arm_dm/python",
    "providers/rebot_arm_integrated/python",
    "providers/rebot_arm_contact/python",
    "providers/rebot_arm_grip/python",
    "skills/contact_work_runtime/python",
    "skills/grip_work_runtime/python",
    "skills/grip/python",
    "skills/slicing/python",
    "skills/grip-object/python",
    "skills/move-carried-object/python",
    "skills/let-go/python",
    "skills/lay-flat/python",
    "skills/limited-graph/python",
    "skills/locate-effector-front/python",
    "skills/observe_pointed_object/python",
    "skills/refine-arm-root-translation/python",
    "skills/register_tool_to_control_frame/python",
    "skills/spatial_registration_rgbd/python",
    "skills/locate_arm_base/python",
    "test_agent/python",
)

COMPILE_PATHS = (
    "contracts/python",
    "providers/orbbec_femto_bolt/python",
    "providers/local_vio/python",
    "providers/arm_scene_compiler/python",
    "providers/sam2_scene_tracker/python",
    "providers/foundation_pose/python",
    "providers/rebot_arm_dm/python",
    "providers/rebot_arm_integrated/python",
    "providers/rebot_arm_contact/python",
    "providers/rebot_arm_grip/python",
    "skills/contact_work_runtime/python",
    "skills/grip_work_runtime/python",
    "skills/grip/python",
    "skills/slicing/python",
    "skills/grip-object/python",
    "skills/move-carried-object/python",
    "skills/let-go/python",
    "skills/lay-flat/python",
    "skills/limited-graph/python",
    "skills/locate-effector-front/python",
    "skills/observe_pointed_object/python",
    "skills/refine-arm-root-translation/python",
    "skills/register_tool_to_control_frame/python",
    "skills/spatial_registration_rgbd/python",
    "skills/locate_arm_base/python",
    "test_agent/python",
)

TEST_PATHS = (
    "contracts/python/tests",
    "providers/orbbec_femto_bolt/python/tests",
    "providers/local_vio/python/tests",
    "providers/arm_scene_compiler/python/tests",
    "providers/sam2_scene_tracker/python/tests",
    "providers/foundation_pose/python/tests",
    "providers/rebot_arm_dm/python/tests",
    "providers/rebot_arm_integrated/python/tests",
    "providers/rebot_arm_contact/python/tests",
    "providers/rebot_arm_grip/python/tests",
    "skills/contact_work_runtime/python/tests",
    "skills/grip_work_runtime/python/tests",
    "skills/grip/python/tests",
    "skills/slicing/python/tests",
    "skills/grip-object/python/tests",
    "skills/let-go/python/tests",
    "skills/lay-flat/python/tests",
    "skills/limited-graph/python/tests",
    "skills/locate-effector-front/python/tests",
    "skills/observe_pointed_object/python/tests",
    "skills/refine-arm-root-translation/python/tests",
    "skills/register_tool_to_control_frame/python/tests",
    "skills/spatial_registration_rgbd/python/tests",
    "skills/locate_arm_base/python/tests",
    "test_agent/python/tests",
)


def _workspace_paths(relative_paths: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for relative_path in relative_paths:
        candidate = (WORKSPACE / relative_path).resolve()
        try:
            candidate.relative_to(WORKSPACE)
        except ValueError as exc:
            raise RuntimeError(
                f"Validation path escapes the workspace: {relative_path}"
            ) from exc
        if not candidate.exists():
            raise RuntimeError(f"Validation path does not exist: {relative_path}")
        paths.append(candidate)
    return paths


def _run(arguments: list[str], *, environment: dict[str, str] | None = None) -> None:
    subprocess.run(
        arguments,
        cwd=WORKSPACE,
        env=environment,
        check=True,
    )


def install() -> None:
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            *DEPENDENCY_SPECS,
        ]
    )
    for package in _workspace_paths(EDITABLE_PACKAGES):
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-e",
                str(package),
            ]
        )


def test() -> None:
    python_paths = _workspace_paths(PYTHON_PATH_ENTRIES)
    compile_paths = _workspace_paths(COMPILE_PATHS)
    test_paths = _workspace_paths(TEST_PATHS)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in python_paths)
    _run(
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            *(str(path) for path in compile_paths),
        ],
        environment=environment,
    )
    _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--import-mode=importlib",
            *(str(path) for path in test_paths),
        ],
        environment=environment,
    )


def build_wheels(wheel_root_argument: str) -> None:
    wheel_root = Path(wheel_root_argument).resolve()
    try:
        wheel_root.relative_to(WORKSPACE)
    except ValueError as exc:
        raise RuntimeError(
            f"Wheel output must remain inside the workspace: {wheel_root}"
        ) from exc
    if wheel_root == WORKSPACE:
        raise RuntimeError("Wheel output cannot be the workspace root.")

    if wheel_root.exists():
        shutil.rmtree(wheel_root)
    wheel_root.mkdir(parents=True)

    for package in _workspace_paths(EDITABLE_PACKAGES):
        package_build_root = package / "build"
        if package_build_root.is_dir():
            shutil.rmtree(package_build_root)
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(wheel_root),
                str(package),
            ]
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the canonical cross-platform Midbrain Python validation."
    )
    parser.add_argument("command", choices=("install", "test", "wheels"))
    parser.add_argument("--wheel-root")
    arguments = parser.parse_args()

    if arguments.command == "install":
        if arguments.wheel_root is not None:
            parser.error("--wheel-root is only valid for the wheels command")
        install()
    elif arguments.command == "test":
        if arguments.wheel_root is not None:
            parser.error("--wheel-root is only valid for the wheels command")
        test()
    else:
        if not arguments.wheel_root:
            parser.error("the wheels command requires --wheel-root")
        build_wheels(arguments.wheel_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
