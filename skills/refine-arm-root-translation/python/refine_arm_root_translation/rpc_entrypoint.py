from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np

from refine_arm_root_translation.manager_state import (
    ManagerCompactAlignmentStore,
)
from refine_arm_root_translation.profile import load_effector_profile
from refine_arm_root_translation.runtime import TranslationRefinementSkill


class LineRpcError(RuntimeError):
    """Preserve structured host failures across the Skill process boundary."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str | None = None,
        status_code: int | None = None,
        response_body: Any = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.response_body = response_body


class LineRpcClient:
    def __init__(self) -> None:
        self._next_id = 1

    async def request(
        self,
        method: str,
        parameters: dict[str, Any],
    ) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message = {
            "type": "request",
            "id": request_id,
            "method": str(method),
            "parameters": parameters,
        }
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            raise RuntimeError("host closed the Skill RPC stream")
        response = json.loads(line)
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise RuntimeError("host returned a mismatched Skill RPC response")
        if response.get("ok") is not True:
            error = response.get("error")
            message = (
                str((error or {}).get("message") or error)
                if isinstance(error, dict)
                else str(error or "host RPC request failed")
            )
            raise LineRpcError(
                message,
                error_type=(
                    str(error.get("type"))
                    if isinstance(error, dict) and error.get("type") is not None
                    else None
                ),
                status_code=(
                    int(error["status_code"])
                    if isinstance(error, dict)
                    and isinstance(error.get("status_code"), int)
                    else None
                ),
                response_body=(
                    error.get("response_body") if isinstance(error, dict) else None
                ),
            )
        return response.get("result")


class RpcManager:
    def __init__(self, rpc: LineRpcClient) -> None:
        self.rpc = rpc

    async def workcell_calibrations(self) -> dict[str, Any]:
        result = await self.rpc.request("manager.workcell_calibrations", {})
        if not isinstance(result, dict):
            raise RuntimeError("Manager calibration catalog must be an object")
        return result

    async def refine_workcell_calibration_translation(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self.rpc.request(
            "manager.refine_workcell_translation",
            {"request": request},
        )
        if not isinstance(result, dict):
            raise RuntimeError("Manager refinement result must be an object")
        return result


class RpcVlmClient:
    def __init__(self, rpc: LineRpcClient, session_dir: Path) -> None:
        self.rpc = rpc
        self.session_dir = session_dir
        self._invocation = 0
        self.model_id = "midbrain.vlm_router"

    async def invoke(
        self,
        *,
        prompt: str,
        images: list[dict[str, Any]],
        purpose: str,
    ) -> str:
        self._invocation += 1
        serialized: list[dict[str, Any]] = []
        for index, image in enumerate(images):
            suffix = self._image_suffix(str(image["media_type"]))
            path = self.session_dir / (
                f"vlm-{self._invocation:02d}-{index:02d}-{image['id']}{suffix}"
            )
            path.write_bytes(bytes(image["image_bytes"]))
            serialized.append(
                {
                    "id": str(image["id"]),
                    "label": str(image["label"]),
                    "path": str(path),
                    "media_type": str(image["media_type"]),
                    "width": int(image["width"]),
                    "height": int(image["height"]),
                }
            )
        result = await self.rpc.request(
            "vlm.invoke",
            {
                "prompt": str(prompt),
                "purpose": str(purpose),
                "images": serialized,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("VLM host returned invalid inference data")
        route = result.get("route")
        if isinstance(route, dict):
            selected_model = str(route.get("model_id") or "").strip()
            if selected_model:
                self.model_id = selected_model
        return result["text"]

    @staticmethod
    def _image_suffix(media_type: str) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }.get(media_type, ".image")


class RpcVisualEvidencePublisher:
    def __init__(self, rpc: LineRpcClient, session_dir: Path) -> None:
        self.rpc = rpc
        self.session_dir = session_dir
        self._publication = 0

    async def register_channels(self, **kwargs: Any) -> dict[str, Any]:
        self._publication += 1
        channels: list[dict[str, Any]] = []
        for index, channel in enumerate(kwargs.get("channels") or []):
            path = self.session_dir / (
                f"evidence-{self._publication:02d}-{index:02d}-{channel['id']}.png"
            )
            path.write_bytes(bytes(channel["image_bytes"]))
            channels.append(
                {
                    **{
                        key: value
                        for key, value in channel.items()
                        if key != "image_bytes"
                    },
                    "path": str(path),
                }
            )
        parameters = {
            **{key: value for key, value in kwargs.items() if key != "channels"},
            "channels": channels,
        }
        result = await self.rpc.request("visual_evidence.register", parameters)
        if not isinstance(result, dict):
            raise RuntimeError("visual evidence host returned invalid data")
        return result


class RpcObservationSource:
    def __init__(
        self,
        rpc: LineRpcClient,
        session_dir: Path,
        state_store: ManagerCompactAlignmentStore,
        profile: dict[str, Any],
    ) -> None:
        self.rpc = rpc
        self.session_dir = session_dir
        self.state_store = state_store
        self.profile = profile

    async def capture(self) -> dict[str, Any]:
        record = self.state_store.active_record
        compatibility = self.profile["robot_compatibility"]
        result = await self.rpc.request(
            "observation.capture",
            {
                "world_frame": record["world_frame"],
                "arm_base_frame": compatibility["arm_base_frame"],
                "controlled_frame": compatibility["controlled_frame"],
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("observation host returned invalid data")
        observation = dict(result)
        observation["rgb"] = self._load_array(result.get("rgb_path"), "rgb")
        observation["registered_depth_m"] = self._load_array(
            result.get("registered_depth_path"),
            "registered depth",
        )
        observation.pop("rgb_path", None)
        observation.pop("registered_depth_path", None)
        observation["identities"] = self.state_store.active_identities
        return observation

    async def revalidate(self, observation: dict[str, Any]) -> dict[str, Any]:
        record = self.state_store.active_record
        compatibility = self.profile["robot_compatibility"]
        result = await self.rpc.request(
            "observation.revalidate",
            {
                "world_frame": record["world_frame"],
                "arm_base_frame": compatibility["arm_base_frame"],
                "controlled_frame": compatibility["controlled_frame"],
                "captured_provenance": observation.get("provenance"),
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("capture-context host returned invalid data")
        return result

    def _load_array(self, path_value: Any, label: str) -> np.ndarray:
        if not isinstance(path_value, str) or not path_value:
            raise RuntimeError(f"observation {label} path is missing")
        path = Path(path_value).resolve()
        session = self.session_dir.resolve()
        if session not in path.parents:
            raise RuntimeError(f"observation {label} path escaped the session directory")
        return np.load(path, allow_pickle=False)


async def run_rpc_skill(profile_path: Path, session_dir: Path) -> dict[str, Any]:
    rpc = LineRpcClient()
    line = await asyncio.to_thread(sys.stdin.readline)
    if not line:
        raise RuntimeError("host did not send a Skill invocation")
    invocation = json.loads(line)
    if not isinstance(invocation, dict) or invocation.get("type") != "invoke":
        raise RuntimeError("first Skill RPC message must be an invocation")
    arguments = invocation.get("arguments")
    if not isinstance(arguments, dict):
        raise RuntimeError("Skill invocation arguments must be an object")
    profile = load_effector_profile(profile_path)
    manager = RpcManager(rpc)

    async def arm_identity(record: dict[str, Any]) -> dict[str, Any]:
        result = await rpc.request(
            "arm.identity",
            {
                "arm_base_frame": profile["robot_compatibility"][
                    "arm_base_frame"
                ],
                "compatibility": profile["robot_compatibility"],
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("arm identity host returned invalid data")
        return result

    state_store = ManagerCompactAlignmentStore(
        manager,
        profile=profile,
        arm_identity_source=arm_identity,
    )
    observation_source = RpcObservationSource(
        rpc,
        session_dir,
        state_store,
        profile,
    )

    async def reference_images(asset_ids: list[str]) -> list[dict[str, Any]]:
        result = await rpc.request(
            "assets.resolve_images",
            {"asset_ids": asset_ids},
        )
        if not isinstance(result, list):
            raise RuntimeError("asset host returned invalid data")
        images: list[dict[str, Any]] = []
        for item in result:
            if not isinstance(item, dict):
                raise RuntimeError("asset host returned an invalid image")
            path = Path(str(item.get("path") or "")).resolve()
            if session_dir.resolve() not in path.parents:
                raise RuntimeError("resolved asset escaped the session directory")
            images.append(
                {
                    **{key: value for key, value in item.items() if key != "path"},
                    "image_bytes": path.read_bytes(),
                }
            )
        return images

    skill = TranslationRefinementSkill(
        profile_path=profile_path,
        observation_source=observation_source.capture,
        state_revalidator=observation_source.revalidate,
        vlm=RpcVlmClient(rpc, session_dir),
        state_store=state_store,
        visual_evidence_publisher=RpcVisualEvidencePublisher(rpc, session_dir),
        reference_image_source=reference_images,
        review_threshold_m=float(
            profile["refinement_policy"][
                "second_vlm_review_raw_delta_threshold_m"
            ]
        ),
        maximum_raw_translation_delta_m=float(
            profile["refinement_policy"][
                "maximum_raw_translation_delta_m"
            ]
        ),
        maximum_adopted_translation_delta_m=float(
            profile["refinement_policy"][
                "maximum_adopted_translation_delta_m"
            ]
        ),
        minimum_confidence=float(
            profile["refinement_policy"]["minimum_landmark_confidence"]
        ),
        minimum_same_surface_confidence=float(
            profile["refinement_policy"][
                "minimum_same_surface_confidence"
            ]
        ),
        maximum_capture_landmark_motion_m=float(
            profile["capture_motion_policy"][
                "maximum_landmark_motion_m"
            ]
        ),
    )
    return await skill.run(
        adoption_factor=arguments.get("adoption_factor", 1.0),
        sample_count=arguments.get("sample_count", 1),
        landmark_id=arguments.get("landmark_id"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--session-dir", required=True)
    options = parser.parse_args()
    profile_path = Path(options.profile).resolve()
    session_dir = Path(options.session_dir).resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = asyncio.run(run_rpc_skill(profile_path, session_dir))
        message = {"type": "result", "ok": True, "result": result}
        exit_code = 0
    except Exception as error:
        message = {
            "type": "result",
            "ok": False,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }
        exit_code = 1
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
