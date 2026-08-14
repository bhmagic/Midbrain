from __future__ import annotations

from typing import Any
import copy
import time


def _text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def evaluate_authority_coordination(
    *,
    resource_id: str,
    manager_available: bool,
    manager_view: dict[str, Any] | None,
    local_basic_lease: dict[str, Any] | None,
    upstream_authority: dict[str, Any] | None,
    local_writer_active: bool,
    motion_inhibited: bool,
) -> dict[str, Any]:
    """Compare advisory Manager authority with Contact's Basic lease lineage."""

    manager_view = copy.deepcopy(manager_view) if isinstance(manager_view, dict) else None
    local_basic_lease = (
        copy.deepcopy(local_basic_lease)
        if isinstance(local_basic_lease, dict)
        else None
    )
    upstream_authority = (
        copy.deepcopy(upstream_authority)
        if isinstance(upstream_authority, dict)
        else None
    )
    manager_lease = (
        manager_view.get("active_lease")
        if isinstance(manager_view, dict)
        else None
    )
    manager_lease = manager_lease if isinstance(manager_lease, dict) else None
    manager_owner = _text((manager_lease or {}).get("owner_id"))
    manager_lease_id = _text((manager_lease or {}).get("lease_id"))
    upstream_owner = _text((upstream_authority or {}).get("owner_id"))
    upstream_lease_id = _text((upstream_authority or {}).get("lease_id"))
    local_held = local_basic_lease is not None
    writer_active = bool(local_writer_active)

    issues: list[str] = []
    if not manager_available:
        issues.append("MANAGER_UNAVAILABLE")
    elif _text((manager_view or {}).get("resource_id")) != _text(resource_id):
        issues.append("MANAGER_RESOURCE_MISMATCH")
    if writer_active and not local_held:
        issues.append("LOCAL_WRITER_WITHOUT_BASIC_LEASE")
    if writer_active and motion_inhibited:
        issues.append("MOTION_INHIBIT_WITH_LOCAL_WRITER")
    if writer_active and manager_available and manager_lease is None:
        issues.append("MANAGER_AUTHORITY_ABSENT_FOR_LOCAL_WRITER")
    if writer_active and manager_lease is not None:
        if upstream_owner is None or upstream_lease_id is None:
            issues.append("AUTHORITY_LINEAGE_NOT_BOUND")
        else:
            if upstream_owner != manager_owner:
                issues.append("UPSTREAM_OWNER_MISMATCH")
            if upstream_lease_id != manager_lease_id:
                issues.append("UPSTREAM_AUTHORITY_LEASE_MISMATCH")

    lineage_bound = bool(
        writer_active
        and manager_lease is not None
        and upstream_owner == manager_owner
        and upstream_lease_id == manager_lease_id
    )
    if not manager_available:
        state = "MANAGER_UNAVAILABLE_WITH_LOCAL_WRITER" if writer_active else "MANAGER_UNAVAILABLE_IDLE"
    elif writer_active and manager_lease is not None:
        state = "COORDINATED_ACTIVE" if lineage_bound and not issues else "DUAL_LAYER_UNCORRELATED"
    elif writer_active:
        state = "PROVIDER_LOCAL_ONLY"
    elif manager_lease is not None and local_held:
        state = "MANAGER_PRESENT_LOCAL_STANDBY"
    elif manager_lease is not None:
        state = "MANAGER_ONLY"
    elif local_held:
        state = "LOCAL_LEASE_STANDBY"
    else:
        state = "NO_ACTIVE_AUTHORITY"

    return {
        "schema": "physical_agent.authority_coordination_state",
        "schema_version": 1,
        "policy_id": "contact.manager_basic.shadow.v1",
        "observed_at_us": time.time_ns() // 1000,
        "resource_id": resource_id,
        "state": state,
        "consistent": not issues,
        "disagreement_reasons": issues,
        "manager_available": bool(manager_available),
        "manager_view": manager_view,
        "local_basic_lease": local_basic_lease,
        "local_writer_active": writer_active,
        "lineage": {
            "bound": lineage_bound,
            "upstream_owner_id": upstream_owner,
            "upstream_authority_lease_id": upstream_lease_id,
            "manager_owner_id": manager_owner,
            "manager_authority_lease_id": manager_lease_id,
        },
        "fencing": {
            "manager_generation": (manager_lease or {}).get("fencing_generation"),
            "manager_namespace": "MANAGER_CONTROL_AUTHORITY_RESOURCE",
            "local_basic_generation": (local_basic_lease or {}).get("fencing_generation"),
            "local_basic_namespace": "BASIC_PROVIDER_OPERATIONAL_LEASE",
            "numeric_equality_has_meaning": False,
        },
        "effect": {
            "mode": "SHADOW_OBSERVE",
            "physical_enforcement": False,
            "may_replace_local_basic_lease": False,
            "may_switch_control_mode": False,
            "may_submit_motor_commands": False,
        },
    }
