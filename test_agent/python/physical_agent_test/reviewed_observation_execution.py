from __future__ import annotations

from typing import Any, Protocol


class AuthorizationStoreProtocol(Protocol):
    def get(self, decision_id: str) -> dict[str, Any]:
        """Return one authorization decision."""

    def issue_execution_assertion(
        self,
        decision_id: str,
    ) -> dict[str, Any]:
        """Issue one decision-bound execution assertion."""


class IntegratedControllerProtocol(Protocol):
    async def stage_scene(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Refresh one exact reviewed semantic scene."""

    async def commit_transit_path(
        self,
        payload: dict[str, Any],
        *,
        authorization_assertion: str,
    ) -> dict[str, Any]:
        """Commit one exact controller preview."""


class ReviewedObservationExecutionAdapter:
    """Commit one exact, separately approved observation preview."""

    def __init__(
        self,
        authorization_store: AuthorizationStoreProtocol,
        integrated: IntegratedControllerProtocol,
    ):
        self.authorization_store = authorization_store
        self.integrated = integrated

    async def run(self, *, decision_id: str) -> dict[str, Any]:
        normalized_decision_id = str(decision_id or "").strip()
        if not normalized_decision_id:
            raise ValueError("decision_id must be non-empty text")

        record = self.authorization_store.get(normalized_decision_id)
        self._validate_record(record)
        authority = record["safety"]["controller_preview_authority"]
        scene = self._reviewed_scene(record, authority)
        scene_refresh = await self.integrated.stage_scene(scene)
        refreshed_revision = (
            (scene_refresh.get("scene") or {}).get("revision")
            if isinstance(scene_refresh, dict)
            else None
        )
        if refreshed_revision != authority["scene_revision"]:
            raise RuntimeError(
                "Integrated did not accept the exact reviewed scene revision"
            )
        issued = self.authorization_store.issue_execution_assertion(
            normalized_decision_id
        )
        result = await self.integrated.commit_transit_path(
            {
                "plan_id": authority["plan_id"],
                "request_sha256": authority["request_sha256"],
                "preview_sha256": authority["preview_sha256"],
                "decision_id": normalized_decision_id,
                "authorization_assertion_sha256": issued[
                    "assertion_sha256"
                ],
            },
            authorization_assertion=issued["assertion"],
        )
        return {
            "schema": (
                "physical_agent.reviewed_observation_motion_execution"
            ),
            "schema_version": 1,
            "decision_id": normalized_decision_id,
            "status": result.get("status"),
            "approval_executes_action": False,
            "model_supplied_motion_parameters": False,
            "reviewed_scene_refreshed": True,
            "scene_revision": refreshed_revision,
            "integrated_controller": result,
        }

    @staticmethod
    def _validate_record(record: dict[str, Any]) -> None:
        if record.get("decision_type") != "PHYSICAL_OBSERVATION_POSE":
            raise RuntimeError(
                "decision is not a physical observation-pose authorization"
            )
        if record.get("status") != "APPROVED":
            raise RuntimeError(
                "reviewed observation execution requires an APPROVED decision"
            )
        safety = record.get("safety")
        if not isinstance(safety, dict):
            raise RuntimeError("approved decision has no safety record")
        if safety.get("approval_executes_action") is not False:
            raise RuntimeError(
                "authorization safety contract must separate approval "
                "from execution"
            )
        authority = safety.get("controller_preview_authority")
        if not isinstance(authority, dict):
            raise RuntimeError(
                "approved decision has no controller preview authority"
            )
        for field in (
            "plan_id",
            "request_sha256",
            "preview_sha256",
            "controller_provider_id",
            "controller_provider_instance_id",
            "controller_boot_id",
            "controller_configuration_sha256",
            "scene_revision",
            "expires_at_us",
        ):
            if authority.get(field) in (None, ""):
                raise RuntimeError(
                    f"controller preview authority is missing {field}"
                )

    @staticmethod
    def _reviewed_scene(
        record: dict[str, Any],
        authority: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = record.get("evidence")
        scene = (
            evidence.get("semantic_scene")
            if isinstance(evidence, dict)
            else None
        )
        if not isinstance(scene, dict):
            raise RuntimeError(
                "approved decision has no exact semantic_scene evidence"
            )
        scene_revision = str(
            scene.get("scene_revision") or scene.get("revision") or ""
        ).strip()
        if scene_revision != authority["scene_revision"]:
            raise RuntimeError(
                "reviewed semantic scene revision does not match preview"
            )
        if scene.get("frame_id") != "rebot_arm_base":
            raise RuntimeError(
                "reviewed semantic scene must use rebot_arm_base"
            )
        spheres = scene.get("spheres")
        if not isinstance(spheres, list):
            raise RuntimeError(
                "reviewed semantic scene spheres must be an array"
            )
        return scene
