from __future__ import annotations

from typing import Any
import copy
import time


AUTHORITY_COORDINATION_SCHEMA = "physical_agent.authority_coordination_state"
AUTHORITY_COORDINATION_SCHEMA_VERSION = 1
AUTHORITY_COORDINATION_POLICY_ID = "integrated.manager_basic.shadow.v1"


def _nonempty(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def evaluate_authority_coordination(
    *,
    resource_id: str,
    manager_available: bool,
    manager_view: dict[str, Any] | None,
    local_basic_lease: dict[str, Any] | None,
    local_writer_active: bool = False,
    integrated_residency: str | None = None,
    integrated_control_state: str | None = None,
    motion_inhibited: bool = False,
    authorization_state: str | None = None,
    upstream_owner_id: str | None = None,
    upstream_authority_lease_id: str | None = None,
    relinquishment_state: str | None = None,
    observed_at_us: int | None = None,
) -> dict[str, Any]:
    """Compare advisory Manager authority with the authoritative Basic lease.

    Manager and Basic fencing generations are intentionally reported as
    separate namespaces. Equality between those counters has no meaning.
    """

    manager_view = (
        copy.deepcopy(manager_view)
        if isinstance(manager_view, dict)
        else None
    )
    local_basic_lease = (
        copy.deepcopy(local_basic_lease)
        if isinstance(local_basic_lease, dict)
        else None
    )
    manager_lease = (
        manager_view.get("active_lease")
        if isinstance(manager_view, dict)
        else None
    )
    manager_lease = manager_lease if isinstance(manager_lease, dict) else None
    manager_active = manager_lease is not None
    local_lease_held = bool((local_basic_lease or {}).get("active"))
    local_writer_active = bool(local_writer_active)

    manager_owner_id = _nonempty((manager_lease or {}).get("owner_id"))
    manager_lease_id = _nonempty((manager_lease or {}).get("lease_id"))
    manager_generation = (manager_lease or {}).get("fencing_generation")
    local_lease_id = _nonempty((local_basic_lease or {}).get("lease_id"))
    local_generation = (local_basic_lease or {}).get("fencing_generation")
    upstream_owner_id = _nonempty(upstream_owner_id)
    upstream_authority_lease_id = _nonempty(upstream_authority_lease_id)

    issues: list[str] = []
    if not manager_available:
        issues.append("MANAGER_UNAVAILABLE")
    elif (
        isinstance(manager_view, dict)
        and _nonempty(manager_view.get("resource_id")) != _nonempty(resource_id)
    ):
        issues.append("MANAGER_RESOURCE_MISMATCH")

    if manager_active and (
        manager_owner_id is None
        or manager_lease_id is None
        or not isinstance(manager_generation, int)
    ):
        issues.append("MANAGER_LEASE_IDENTITY_INCOMPLETE")
    if local_lease_held and (
        local_lease_id is None or not isinstance(local_generation, int)
    ):
        issues.append("LOCAL_BASIC_LEASE_IDENTITY_INCOMPLETE")
    if local_writer_active and not local_lease_held:
        issues.append("LOCAL_WRITER_WITHOUT_BASIC_LEASE")
    if motion_inhibited and local_writer_active:
        issues.append("MOTION_INHIBIT_WITH_LOCAL_WRITER")
    if (
        local_writer_active
        and _nonempty(relinquishment_state)
        not in {None, "IDLE", "COMPLETED"}
    ):
        issues.append("RELINQUISHMENT_PENDING_WITH_LOCAL_WRITER")

    lineage_bound = False
    if manager_active and local_writer_active:
        if upstream_owner_id is None or upstream_authority_lease_id is None:
            issues.append("AUTHORITY_LINEAGE_NOT_BOUND")
        else:
            if upstream_owner_id != manager_owner_id:
                issues.append("UPSTREAM_OWNER_MISMATCH")
            if upstream_authority_lease_id != manager_lease_id:
                issues.append("UPSTREAM_AUTHORITY_LEASE_MISMATCH")
            lineage_bound = (
                upstream_owner_id == manager_owner_id
                and upstream_authority_lease_id == manager_lease_id
            )
    elif local_writer_active and manager_available:
        issues.append("MANAGER_AUTHORITY_ABSENT_FOR_LOCAL_WRITER")

    if not manager_available:
        state = (
            "MANAGER_UNAVAILABLE_WITH_LOCAL_LEASE"
            if local_writer_active
            else (
                "MANAGER_UNAVAILABLE_LOCAL_STANDBY"
                if local_lease_held
                else "MANAGER_UNAVAILABLE_IDLE"
            )
        )
    elif manager_active and local_writer_active:
        state = (
            "COORDINATED_ACTIVE"
            if lineage_bound and not issues
            else "DUAL_LAYER_UNCORRELATED"
        )
    elif local_writer_active:
        state = "PROVIDER_LOCAL_ONLY"
    elif manager_active and local_lease_held:
        state = "MANAGER_PRESENT_LOCAL_STANDBY"
    elif manager_active:
        state = "MANAGER_ONLY"
    elif local_lease_held:
        state = "LOCAL_LEASE_STANDBY"
    else:
        state = "NO_ACTIVE_AUTHORITY"

    return {
        "schema": AUTHORITY_COORDINATION_SCHEMA,
        "schema_version": AUTHORITY_COORDINATION_SCHEMA_VERSION,
        "policy_id": AUTHORITY_COORDINATION_POLICY_ID,
        "observed_at_us": int(observed_at_us or time.time_ns() // 1000),
        "resource_id": str(resource_id),
        "state": state,
        "consistent": not issues,
        "disagreement_reasons": issues,
        "manager_available": bool(manager_available),
        "manager_view": manager_view,
        "local_basic_lease": local_basic_lease,
        "local_basic_lease_held": local_lease_held,
        "local_writer_active": local_writer_active,
        "lineage": {
            "bound": lineage_bound,
            "upstream_owner_id": upstream_owner_id,
            "upstream_authority_lease_id": upstream_authority_lease_id,
            "manager_owner_id": manager_owner_id,
            "manager_authority_lease_id": manager_lease_id,
        },
        "fencing": {
            "manager_generation": manager_generation,
            "manager_namespace": "MANAGER_CONTROL_AUTHORITY_RESOURCE",
            "local_basic_generation": local_generation,
            "local_basic_namespace": "BASIC_PROVIDER_OPERATIONAL_LEASE",
            "numeric_equality_has_meaning": False,
        },
        "context": {
            "integrated_residency": _nonempty(integrated_residency),
            "integrated_control_state": _nonempty(integrated_control_state),
            "local_writer_active": local_writer_active,
            "motion_inhibited": bool(motion_inhibited),
            "authorization_state": _nonempty(authorization_state),
            "relinquishment_state": _nonempty(relinquishment_state),
        },
        "effect": {
            "mode": "SHADOW_OBSERVE",
            "physical_enforcement": False,
            "may_replace_local_basic_lease": False,
            "may_switch_control_mode": False,
            "may_submit_motor_commands": False,
        },
    }
