from __future__ import annotations

from dataclasses import dataclass


TRANSIT_SPEED = "TRANSIT_SPEED"
CONTACT_WORK = "CONTACT_WORK"
PRESS_MIT = "PRESS_MIT"
SUPPORTED_EXECUTION_MODES = {TRANSIT_SPEED, CONTACT_WORK, PRESS_MIT}

BASIC_POS_VEL = "POSITION_VELOCITY_LIMITED"
BASIC_POS_TOR = "POSITION_EFFORT_LIMITED"
BASIC_MIT = "IMPEDANCE"

TRANSIT_BACKEND_IMPEDANCE = "IMPEDANCE"
TRANSIT_BACKEND_POS_SPEED = "POS_SPEED"
SUPPORTED_TRANSIT_BACKENDS = {
    TRANSIT_BACKEND_IMPEDANCE,
    TRANSIT_BACKEND_POS_SPEED,
}


@dataclass(frozen=True)
class ExecutionModeSpec:
    name: str
    basic_mode: str
    requires_contact_baseline: bool
    intended_contact: bool
    command_strategy: str
    description: str


MODE_SPECS = {
    TRANSIT_SPEED: ExecutionModeSpec(
        name=TRANSIT_SPEED,
        basic_mode=BASIC_POS_VEL,
        requires_contact_baseline=False,
        intended_contact=False,
        command_strategy="LATCHED_ENDPOINT_HELD_UNTIL_OPERATOR_RELEASE",
        description="POS_VEL endpoint approach and hold with no automatic motor-mode handoff.",
    ),
    CONTACT_WORK: ExecutionModeSpec(
        name=CONTACT_WORK,
        basic_mode=BASIC_POS_TOR,
        requires_contact_baseline=True,
        intended_contact=True,
        command_strategy="LATCHED_ENDPOINT_KEEPALIVE",
        description="Short 6-DoF work motion with an explicit torque baseline and task limits.",
    ),
    PRESS_MIT: ExecutionModeSpec(
        name=PRESS_MIT,
        basic_mode=BASIC_MIT,
        requires_contact_baseline=False,
        intended_contact=True,
        command_strategy="STREAMED_SETPOINT",
        description="Operator-authorized compliant pressing in a known direction.",
    ),
}


def normalize_execution_mode(value: str) -> str:
    mode = str(value).strip().upper()
    if mode not in SUPPORTED_EXECUTION_MODES:
        raise ValueError(f"unsupported execution mode {mode}")
    return mode


def normalize_transit_backend(value: str | None) -> str:
    backend = str(value or TRANSIT_BACKEND_IMPEDANCE).strip().upper()
    if backend not in SUPPORTED_TRANSIT_BACKENDS:
        raise ValueError(
            "execution_backend must be IMPEDANCE or POS_SPEED"
        )
    return backend
