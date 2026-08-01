from __future__ import annotations

from typing import Any, Protocol


class BasicSafeHomeClientProtocol(Protocol):
    async def state(self) -> dict[str, Any]:
        """Return the current Basic controller state."""

    async def safe_home(self) -> dict[str, Any]:
        """Run the Basic controller's configured safe-home operation."""


class BasicSafeHomeAdapter:
    """Invoke and report one controller-owned safe-home operation."""

    def __init__(self, client: BasicSafeHomeClientProtocol):
        self.client = client

    async def execute(self) -> dict[str, Any]:
        before = await self.client.state()
        provider_state = str(before.get("provider_state") or "UNKNOWN")
        if provider_state == "DISCONNECTED":
            raise RuntimeError(
                "Basic Controller must be running before safe-home; activate "
                "robot_arm.rebot_dm and retry"
            )
        result = await self.client.safe_home()
        details = result.get("details")
        details = details if isinstance(details, dict) else {}
        completed = (
            result.get("success") is True
            and details.get("success") is True
            and details.get("active") is not True
        )
        return {
            "status": (
                "SAFE_HOME_COMPLETED"
                if completed
                else "SAFE_HOME_FAILED"
            ),
            "physical_motion_requested": True,
            "physical_motion_completed": completed,
            "provider_state_before": provider_state,
            "details": details,
            "integrated_controller_recovery": {
                "expected_after_safe_home_preemption": True,
                "physical_motion_authorized": False,
                "required_before_next_integrated_preview": (
                    "EXPLICIT_APPROVED_HOT"
                ),
                "reason": (
                    "Safe-home is owned by Basic and preempts any Integrated "
                    "Basic-controller lease."
                ),
            },
            "message": (
                "The Basic Controller confirmed safe-home completion. A "
                "later Integrated motion must explicitly recover Integrated "
                "to HOT before creating a fresh preview."
                if completed
                else "The Basic Controller did not confirm safe-home "
                f"completion: {details.get('reason') or 'unknown reason'}."
            ),
        }
