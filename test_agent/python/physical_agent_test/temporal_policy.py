from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import time
from typing import Any


@dataclass(frozen=True)
class ObservationTemporalEvidence:
    policy_id: str
    observation_name: str
    observed_at_us: int
    evaluated_at_us: int
    source_age_ms: float
    received_at_us: int | None
    receipt_age_ms: float | None
    transport_delay_ms: float | None
    clock_domain: str | None
    maximum_source_age_ms: float | None
    producer_recommended_max_age_ms: float | None
    hard_expires_at_us: int | None
    decision: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_observation_temporal_policy(
    *,
    observation_name: str,
    observation: dict[str, Any] | None,
    policy_id: str,
    maximum_source_age_ms: float | None,
    now_us: int | None = None,
    maximum_future_skew_ms: float = 1000.0,
) -> ObservationTemporalEvidence:
    """Apply one Skill-owned temporal policy to a Fabric observation."""

    if not isinstance(observation, dict):
        raise RuntimeError(f"{observation_name} observation is unavailable")
    if observation.get("valid") is False:
        raise RuntimeError(f"{observation_name} observation is marked invalid")

    evaluated_at_us = int(now_us or time.time_ns() // 1000)
    observed_at_us = int(observation.get("observed_at_us") or 0)
    if observed_at_us <= 0:
        raise RuntimeError(
            f"{observation_name} observation has no source timestamp"
        )

    hard_expires_at_us = _optional_positive_int(
        observation.get("expires_at_us")
    )
    if (
        hard_expires_at_us is not None
        and evaluated_at_us > hard_expires_at_us
    ):
        raise RuntimeError(
            f"{observation_name} observation exceeded producer hard expiry"
        )

    raw_source_age_ms = (evaluated_at_us - observed_at_us) / 1000.0
    if raw_source_age_ms < -abs(float(maximum_future_skew_ms)):
        raise RuntimeError(
            f"{observation_name} observation source time is in the future"
        )
    source_age_ms = max(0.0, raw_source_age_ms)
    maximum_age = (
        None
        if maximum_source_age_ms is None
        else float(maximum_source_age_ms)
    )
    if maximum_age is not None:
        if maximum_age <= 0.0:
            raise ValueError("maximum_source_age_ms must be positive")
        if source_age_ms > maximum_age:
            raise RuntimeError(
                f"{observation_name} observation exceeds Skill temporal "
                f"policy {policy_id}: age={source_age_ms:.1f} ms, "
                f"limit={maximum_age:.1f} ms"
            )

    received_at_us = _received_at_us(observation.get("received_at"))
    receipt_age_ms = (
        None
        if received_at_us is None
        else max(0.0, (evaluated_at_us - received_at_us) / 1000.0)
    )
    transport_delay_ms = (
        None
        if received_at_us is None
        else (received_at_us - observed_at_us) / 1000.0
    )
    producer_recommended = _optional_float(observation.get("freshness_ms"))

    return ObservationTemporalEvidence(
        policy_id=str(policy_id),
        observation_name=str(observation_name),
        observed_at_us=observed_at_us,
        evaluated_at_us=evaluated_at_us,
        source_age_ms=source_age_ms,
        received_at_us=received_at_us,
        receipt_age_ms=receipt_age_ms,
        transport_delay_ms=transport_delay_ms,
        clock_domain=(
            str(observation.get("clock_domain"))
            if observation.get("clock_domain") is not None
            else None
        ),
        maximum_source_age_ms=maximum_age,
        producer_recommended_max_age_ms=producer_recommended,
        hard_expires_at_us=hard_expires_at_us,
        decision="ACCEPT",
    )


def _received_at_us(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("observation has invalid Fabric receipt time") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1_000_000)


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
