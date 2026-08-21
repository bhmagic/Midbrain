from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid
from typing import Any, Protocol


SCENE_SEGMENTATION_POLICY_SCHEMA = (
    "physical_agent.arm_scene_segmentation_policy"
)
SCENE_SEGMENTATION_POLICY_STREAM = "robot_arm.scene.segmentation_policy"
SUPPORTED_TYPES = {"KEEP_OUT", "PUSHABLE", "WORK_OBJECT"}
PERSISTED_POLICY_SCHEMA = (
    "physical_agent.persisted_arm_scene_segmentation_policy"
)


class FabricPublishProtocol(Protocol):
    async def publish(self, observation: dict[str, Any]) -> dict[str, Any]: ...


class SceneSegmentationPolicyPublisher:
    """Publish an explicit user/upstream scene policy to the Fabric."""

    def __init__(
        self,
        fabric: FabricPublishProtocol,
        *,
        provider_id: str = "test_agent.scene_policy",
        stream: str = SCENE_SEGMENTATION_POLICY_STREAM,
        state_path: Path | None = None,
    ) -> None:
        self.fabric = fabric
        self.provider_id = str(provider_id)
        self.stream = str(stream)
        self.instance_id = str(uuid.uuid4())
        self.boot_id = str(uuid.uuid4())
        self.sequence = 0
        self.state_path = (
            state_path.resolve() if state_path is not None else None
        )

    @staticmethod
    def _normalize_objects(objects: Any) -> list[dict[str, str]]:
        if not isinstance(objects, list) or not objects:
            raise ValueError("scene policy objects must be a non-empty list")
        output: list[dict[str, str]] = []
        seen: set[str] = set()
        for index, raw in enumerate(objects):
            if not isinstance(raw, dict):
                raise ValueError(f"scene policy object {index} must be an object")
            object_id = str(raw.get("object_id") or "").strip()
            description = str(raw.get("description") or "").strip()
            object_type = str(raw.get("type") or "").strip().upper()
            if not object_id or object_id in seen:
                raise ValueError("scene policy object_id values must be unique")
            if not description:
                raise ValueError(
                    f"scene policy object {object_id!r} requires a description"
                )
            if object_type not in SUPPORTED_TYPES:
                raise ValueError(
                    f"scene policy object {object_id!r} has unsupported type"
                )
            seen.add(object_id)
            output.append(
                {
                    "object_id": object_id,
                    "type": object_type,
                    "description": description,
                }
            )
        return output

    async def publish_policy(
        self,
        *,
        policy_id: str,
        objects: Any,
        arm_description: str,
    ) -> dict[str, Any]:
        identifier = str(policy_id or "").strip()
        if not identifier:
            raise ValueError("scene policy requires policy_id")
        arm = str(arm_description or "").strip()
        if not arm:
            raise ValueError("scene policy requires arm_description")
        normalized = self._normalize_objects(objects)
        canonical = {
            "policy_id": identifier,
            "objects": normalized,
            "arm_description": arm,
            # Every explicit map/remap request starts a new fusion epoch, even
            # when the user repeats the same wording. Reusing a content-only
            # revision could otherwise retain geometry from a rejected map.
            "mapping_epoch": str(uuid.uuid4()),
        }
        return await self._publish_canonical(canonical, persist=True)

    async def restore_policy(self) -> dict[str, Any]:
        """Republish the last explicit user-authored policy after restart."""

        if self.state_path is None or not self.state_path.is_file():
            return {
                "status": "NO_PERSISTED_POLICY",
                "stream": self.stream,
                "restored": False,
            }
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "persisted scene policy cannot be read"
            ) from error
        if (
            payload.get("schema") != PERSISTED_POLICY_SCHEMA
            or payload.get("schema_version") != 1
        ):
            raise RuntimeError("persisted scene policy schema is unsupported")
        policy = payload.get("policy")
        if not isinstance(policy, dict):
            raise RuntimeError("persisted scene policy has no policy object")
        canonical = {
            "policy_id": str(policy.get("policy_id") or "").strip(),
            "objects": self._normalize_objects(policy.get("objects")),
            "arm_description": str(
                policy.get("arm_description") or ""
            ).strip(),
            "mapping_epoch": str(
                policy.get("mapping_epoch") or uuid.uuid4()
            ).strip(),
        }
        if not canonical["policy_id"] or not canonical["arm_description"]:
            raise RuntimeError("persisted scene policy is incomplete")
        return await self._publish_canonical(canonical, persist=False)

    async def _publish_canonical(
        self,
        canonical: dict[str, Any],
        *,
        persist: bool,
    ) -> dict[str, Any]:
        identifier = str(canonical["policy_id"])
        normalized = list(canonical["objects"])
        arm = str(canonical["arm_description"])
        mapping_epoch = str(canonical.get("mapping_epoch") or "").strip()
        if not mapping_epoch:
            raise ValueError("scene policy requires mapping_epoch")
        revision = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]
        now_us = time.time_ns() // 1000
        self.sequence += 1
        observation = {
            "schema": SCENE_SEGMENTATION_POLICY_SCHEMA,
            "schema_version": 1,
            "stream": self.stream,
            "provider_id": self.provider_id,
            "provider_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "observed_at_us": now_us,
            "coordinate_frame": "image_screen",
            "clock_domain": "system",
            "valid": True,
            "data": {
                "contract_version": 1,
                "policy_id": identifier,
                "revision": revision,
                "objects": normalized,
                "arm_description": arm,
            },
        }
        accepted = await self.fabric.publish(observation)
        if persist and accepted.get("accepted") is True:
            self._persist_policy(canonical, saved_at_us=now_us)
        return {
            "status": "PUBLISHED",
            "stream": self.stream,
            "sequence": self.sequence,
            "policy_id": identifier,
            "revision": revision,
            "blocking_objects": [
                value["object_id"]
                for value in normalized
                if value["type"] == "KEEP_OUT"
            ],
            "unclaimed_visible_type": "PUSHABLE",
            "pushable_collision_policy": "IGNORE",
            "restored": not persist,
            "fabric_result": accepted,
        }

    def _persist_policy(
        self,
        canonical: dict[str, Any],
        *,
        saved_at_us: int,
    ) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": PERSISTED_POLICY_SCHEMA,
            "schema_version": 1,
            "saved_at_us": int(saved_at_us),
            "policy": canonical,
        }
        temporary = self.state_path.with_suffix(
            self.state_path.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)
