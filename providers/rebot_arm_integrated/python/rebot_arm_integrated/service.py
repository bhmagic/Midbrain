from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import copy
import hashlib
import json
import mimetypes
import os
import subprocess
import threading
import time
import uuid

from .authorization import verify_transit_execution_assertion
from .authority_state import evaluate_authority_coordination
from .control_audit import ControlAuditOutbox
from .controller import IntegratedController
from .platform import PlatformPublisher


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IntegratedService:
    def __init__(
        self,
        controller: IntegratedController,
        config: dict[str, Any],
        manager_url: str | None,
        fabric_url: str | None,
    ):
        self.controller = controller
        self.config = config
        self.shutdown_event = threading.Event()
        self.httpd: ThreadingHTTPServer | None = None
        self.control_url = f"http://{config['listen_host']}:{config['listen_port']}"
        self.provider_root = Path(__file__).resolve().parents[2]
        self.platform = PlatformPublisher(
            config["provider_id"], manager_url, fabric_url, self.control_url
        )
        self.control_audit = ControlAuditOutbox(
            self.provider_root,
            config["provider_id"],
            self.platform.instance_id,
            self.platform.boot_id,
            config.get("control_audit"),
        )
        self.authorization_secret = os.getenv(
            "MIDBRAIN_AUTHORIZATION_SECRET",
            "",
        )
        configure_authorization = getattr(
            self.controller,
            "set_authorization_assertion_configured",
            None,
        )
        if callable(configure_authorization):
            configure_authorization(
                len(self.authorization_secret.encode("utf-8")) >= 32
            )
        self.transit_plan_lock = threading.Lock()
        self.transit_plans: dict[str, dict[str, Any]] = {}
        self.consumed_authorization_assertion_ids: set[str] = set()
        self.web_root = Path(__file__).with_name("web")
        self.publish_thread: threading.Thread | None = None
        self.manager_registered = False
        self.fabric_ready = False
        self.motion_inhibited = False
        self.motion_inhibit_owners: list[dict[str, Any]] = []
        authority_config = config.get("manager_authority", {})
        self.manager_authority_status: dict[str, Any] = {
            "enabled": bool(authority_config.get("enabled", True)),
            "mode": str(authority_config.get("mode", "SHADOW_OBSERVE")),
            "resource_id": str(
                authority_config.get("resource_id", "robot_arm.primary")
            ),
            "enforcement": "ADVISORY",
            "physical_enforcement": False,
            "may_replace_local_basic_lease": False,
            "may_switch_control_mode": False,
            "may_submit_motor_commands": False,
            "safety_boundary": (
                "OBSERVATION_ONLY_LOCAL_BASIC_LEASE_REMAINS_AUTHORITATIVE"
            ),
            "comparison": "WAITING",
            "manager_view": None,
            "local_basic_lease": None,
            "last_error": None,
            "evaluation": None,
            "metrics": {
                "poll_count": 0,
                "transition_count": 0,
                "state_counts": {},
                "disagreement_counts": {},
                "last_state": None,
                "last_transition_at_us": None,
            },
        }
        self.request_results: dict[str, dict[str, Any]] = {}
        self.request_lock = threading.Lock()
        self.safe_termination_lock = threading.Lock()
        self.safe_termination: dict[str, Any] = {
            "state": "IDLE",
            "message": "",
            "started_at_monotonic": None,
            "manager_shadow_plan": None,
        }
        self.fabric_input_last_key: tuple[str, str, int] | None = None
        self.fabric_input_status: dict[str, Any] = {
            "enabled": bool(config.get("fabric_input", {}).get("enabled", False)),
            "stream": str(config.get("fabric_input", {}).get("stream", "")),
            "schema": str(config.get("fabric_input", {}).get("schema", "")),
            "last_result": "WAITING",
            "last_error": None,
            "last_sequence": None,
            "last_provider_id": None,
            "last_age_ms": None,
            "accepted_count": 0,
            "stale_count": 0,
            "rejected_count": 0,
        }
        self.scene_input_last_key: tuple[str, str, int] | None = None
        self.scene_input_status: dict[str, Any] = {
            "enabled": bool(config.get("scene_input", {}).get("enabled", False)),
            "stream": str(config.get("scene_input", {}).get("stream", "")),
            "schema": str(config.get("scene_input", {}).get("schema", "")),
            "last_result": "WAITING",
            "last_error": None,
            "last_sequence": None,
            "last_age_ms": None,
            "accepted_count": 0,
            "stale_count": 0,
            "rejected_count": 0,
            "physical_motion_authorized": False,
        }

    def _sync_platform_state(self) -> None:
        self.controller.update_platform_status(
            self.manager_registered,
            self.fabric_ready,
            self.platform.errors(),
            motion_inhibited=self.motion_inhibited,
            motion_inhibit_owners=self.motion_inhibit_owners,
        )

    def start(self) -> None:
        self.shutdown_event.clear()
        start_hot = False
        try:
            inhibit = self.platform.motion_inhibit()
            self.motion_inhibited = bool(inhibit.get("inhibited", False))
            owners = inhibit.get("owners", [])
            self.motion_inhibit_owners = owners if isinstance(owners, list) else []
            start_hot = not self.motion_inhibited
        except Exception as exc:
            self.manager_registered = False
            self.motion_inhibited = True
            self.motion_inhibit_owners = [
                {
                    "owner_id": "integrated:startup-manager-unavailable",
                    "reason": f"Manager motion-inhibit query failed: {exc}",
                }
            ]
        self._sync_platform_state()
        self.controller.start(hot=start_hot)
        try:
            self.httpd = ThreadingHTTPServer(
                (self.config["listen_host"], int(self.config["listen_port"])),
                self._handler(),
            )
            self.httpd.daemon_threads = True
            threading.Thread(
                target=self.httpd.serve_forever,
                name="staged-http",
                daemon=True,
            ).start()
        except Exception as operation_error:
            self.controller.stop()
            raise

        try:
            self.platform.register(self.controller.snapshot())
            self.manager_registered = self.platform.manager_url is not None
        except Exception as exc:
            self.manager_registered = False
            print(f"[staged-platform] Manager registration deferred: {exc}")
        self._sync_platform_state()

        self.publish_thread = threading.Thread(
            target=self._publish_loop,
            name="staged-platform",
            daemon=True,
        )
        self.publish_thread.start()

    def _publish_loop(self) -> None:
        inhibit_poll_s = max(
            0.10,
            float(self.config["platform"].get("motion_inhibit_poll_ms", 200)) / 1000.0,
        )
        next_register = 0.0
        next_heartbeat = 0.0
        next_status = 0.0
        next_target = 0.0
        next_inhibit = 0.0
        next_authority = 0.0
        next_fabric_input = 0.0
        next_scene_input = 0.0
        fabric_input_cfg = self.config.get("fabric_input", {})
        fabric_input_poll_s = max(0.02, float(fabric_input_cfg.get("poll_ms", 50)) / 1000.0)
        scene_input_cfg = self.config.get("scene_input", {})
        scene_input_poll_s = max(0.02, float(scene_input_cfg.get("poll_ms", 100)) / 1000.0)
        manager_authority_cfg = self.config.get("manager_authority", {})
        authority_poll_s = max(
            0.10,
            float(manager_authority_cfg.get("poll_ms", 500)) / 1000.0,
        )

        while not self.shutdown_event.wait(0.02):
            now = time.monotonic()
            state = self.controller.snapshot()

            if not self.manager_registered and now >= next_register:
                try:
                    self.platform.register(state)
                    self.manager_registered = self.platform.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_register = now + 2.0

            if now >= next_heartbeat:
                try:
                    self.platform.heartbeat(state)
                    self.manager_registered = self.platform.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_heartbeat = now + 1.0

            if now >= next_inhibit:
                try:
                    inhibit = self.platform.motion_inhibit()
                    self.motion_inhibited = bool(inhibit.get("inhibited", False))
                    owners = inhibit.get("owners", [])
                    self.motion_inhibit_owners = owners if isinstance(owners, list) else []
                except Exception:
                    self.manager_registered = False
                next_inhibit = now + inhibit_poll_s

            if (
                bool(manager_authority_cfg.get("enabled", True))
                and now >= next_authority
            ):
                self._poll_advisory_authority(state)
                next_authority = now + authority_poll_s

            if bool(fabric_input_cfg.get("enabled", False)) and now >= next_fabric_input:
                self._consume_fabric_input()
                next_fabric_input = now + fabric_input_poll_s
            if bool(scene_input_cfg.get("enabled", False)) and now >= next_scene_input:
                self._consume_scene_input()
                next_scene_input = now + scene_input_poll_s

            if now >= next_status:
                try:
                    self.platform.publish(
                        "robot_arm.integrated.status",
                        "physical_agent.arm_integrated_mit_bringup_state",
                        state,
                    )
                    self.fabric_ready = self.platform.fabric_url is not None
                except Exception:
                    self.fabric_ready = False
                next_status = now + 0.2

            if now >= next_target:
                try:
                    target = {
                        "control_mode": state.get("control_mode"),
                        "control_state": state.get("control_state"),
                        "target": state.get("target"),
                        "joint_state": state.get("joint_state"),
                        "units": state.get("units"),
                    }
                    self.platform.publish(
                        "robot_arm.integrated.control_target",
                        "physical_agent.arm_control_target",
                        target,
                        freshness_ms=300,
                    )
                    self.fabric_ready = self.platform.fabric_url is not None
                except Exception:
                    self.fabric_ready = False
                next_target = now + 0.2

            if self.platform.fabric_url:
                self.control_audit.publish_pending(
                    lambda stream, event: self.platform.publish(
                        stream,
                        "physical_agent.control_audit_event",
                        event,
                        frame_id=None,
                        freshness_ms=86_400_000,
                    )
                )

            self._sync_platform_state()

    def _consume_fabric_input(self) -> None:
        cfg = self.config.get("fabric_input", {})
        stream = str(cfg.get("stream", "")).strip()
        expected_schema = str(cfg.get("schema", "")).strip()
        try:
            observation = self.platform.latest(stream)
            if observation is None:
                self.fabric_input_status["last_result"] = "NO_OBSERVATION"
                self.fabric_input_status["last_error"] = None
                return
            if expected_schema and str(observation.get("schema", "")) != expected_schema:
                raise ValueError(
                    f"Fabric input schema {observation.get('schema')!r} does not match {expected_schema!r}"
                )
            if observation.get("valid") is False:
                self.fabric_input_status["last_result"] = "INVALID_IGNORED"
                self.fabric_input_status["last_error"] = None
                return

            now_us = time.time_ns() // 1000
            observed_at_us = int(observation.get("observed_at_us") or 0)
            age_ms = None if observed_at_us <= 0 else max(0.0, (now_us - observed_at_us) / 1000.0)
            configured_max_age_ms = float(cfg.get("max_age_ms", 650))
            observation_freshness = observation.get("freshness_ms")
            allowed_age_ms = configured_max_age_ms
            if observation_freshness is not None:
                allowed_age_ms = min(allowed_age_ms, float(observation_freshness))
            expires_at_us = int(observation.get("expires_at_us") or 0)
            if (age_ms is not None and age_ms > allowed_age_ms) or (expires_at_us > 0 and now_us > expires_at_us):
                self.fabric_input_status["last_result"] = "STALE_IGNORED"
                self.fabric_input_status["last_error"] = None
                self.fabric_input_status["last_age_ms"] = age_ms
                self.fabric_input_status["stale_count"] += 1
                return

            key = (
                str(observation.get("provider_instance_id") or observation.get("provider_id") or ""),
                str(observation.get("boot_id") or ""),
                int(observation.get("sequence") or 0),
            )
            if key == self.fabric_input_last_key:
                self.fabric_input_status["last_result"] = "DUPLICATE"
                self.fabric_input_status["last_age_ms"] = age_ms
                return
            data = observation.get("data")
            if not isinstance(data, dict):
                raise ValueError("Fabric arm command data must be an object")
            result = self.controller.stage_external_command(
                data,
                source=f"fabric:{stream}",
                metadata={
                    "schema": observation.get("schema"),
                    "provider_id": observation.get("provider_id"),
                    "provider_instance_id": observation.get("provider_instance_id"),
                    "boot_id": observation.get("boot_id"),
                    "sequence": observation.get("sequence"),
                    "observed_at_us": observed_at_us,
                    "related_skill_id": observation.get("related_skill_id"),
                },
            )
            self.fabric_input_last_key = key
            self.fabric_input_status["last_result"] = "ACCEPTED"
            self.fabric_input_status["last_error"] = None
            self.fabric_input_status["last_sequence"] = key[2]
            self.fabric_input_status["last_provider_id"] = observation.get("provider_id")
            self.fabric_input_status["last_age_ms"] = age_ms
            self.fabric_input_status["accepted_count"] += 1
            self.fabric_input_status["physical_motion_authorized"] = bool(
                result.get("physical_motion_authorized", False)
            )
            self.platform.fabric_consume_error = None
        except Exception as exc:
            self.fabric_input_status["last_result"] = "REJECTED"
            self.fabric_input_status["last_error"] = str(exc)
            self.fabric_input_status["rejected_count"] += 1
            self.platform.fabric_consume_error = str(exc)

    def _consume_scene_input(self) -> None:
        cfg = self.config.get("scene_input", {})
        stream = str(cfg.get("stream", "")).strip()
        expected_schema = str(cfg.get("schema", "")).strip()
        try:
            observation = self.platform.latest(stream)
            if observation is None:
                self.scene_input_status["last_result"] = "NO_OBSERVATION"
                self.scene_input_status["last_error"] = None
                return
            if expected_schema and str(observation.get("schema", "")) != expected_schema:
                raise ValueError(f"Fabric scene schema {observation.get('schema')!r} does not match {expected_schema!r}")
            if observation.get("valid") is False:
                self.scene_input_status["last_result"] = "INVALID_IGNORED"
                return
            now_us = time.time_ns() // 1000
            observed_at_us = int(observation.get("observed_at_us") or 0)
            age_ms = None if observed_at_us <= 0 else max(0.0, (now_us - observed_at_us) / 1000.0)
            allowed_age_ms = float(cfg.get("max_age_ms", 1000))
            if observation.get("freshness_ms") is not None:
                allowed_age_ms = min(allowed_age_ms, float(observation["freshness_ms"]))
            expires_at_us = int(observation.get("expires_at_us") or 0)
            if (age_ms is not None and age_ms > allowed_age_ms) or (expires_at_us > 0 and now_us > expires_at_us):
                self.scene_input_status["last_result"] = "STALE_IGNORED"
                self.scene_input_status["last_age_ms"] = age_ms
                self.scene_input_status["stale_count"] += 1
                return
            key = (
                str(observation.get("provider_instance_id") or observation.get("provider_id") or ""),
                str(observation.get("boot_id") or ""),
                int(observation.get("sequence") or 0),
            )
            if key == self.scene_input_last_key:
                self.scene_input_status["last_result"] = "DUPLICATE"
                self.scene_input_status["last_age_ms"] = age_ms
                return
            data = observation.get("data")
            if not isinstance(data, dict):
                raise ValueError("Fabric semantic scene data must be an object")
            self.controller.stage_scene(data, source=f"fabric:{stream}")
            self.scene_input_last_key = key
            self.scene_input_status["last_result"] = "ACCEPTED"
            self.scene_input_status["last_error"] = None
            self.scene_input_status["last_sequence"] = key[2]
            self.scene_input_status["last_age_ms"] = age_ms
            self.scene_input_status["accepted_count"] += 1
        except Exception as exc:
            self.scene_input_status["last_result"] = "REJECTED"
            self.scene_input_status["last_error"] = str(exc)
            self.scene_input_status["rejected_count"] += 1
            self.platform.fabric_consume_error = str(exc)

    def _state_payload(self) -> dict[str, Any]:
        with self.safe_termination_lock:
            termination = copy.deepcopy(self.safe_termination)
        return {
            **self.controller.snapshot(),
            "safe_termination": termination,
            "fabric_input": copy.deepcopy(self.fabric_input_status),
            "scene_input": copy.deepcopy(self.scene_input_status),
            "manager_authority": copy.deepcopy(self.manager_authority_status),
            "control_audit": self.control_audit.status(),
        }

    def _poll_advisory_authority(self, controller_state: dict[str, Any]) -> None:
        resource_id = str(self.manager_authority_status["resource_id"])
        local_lease = copy.deepcopy(controller_state.get("lease"))
        planning = controller_state.get("planning")
        planning = planning if isinstance(planning, dict) else {}
        authorized_transit = planning.get("authorized_transit")
        authorized_transit = (
            authorized_transit
            if isinstance(authorized_transit, dict)
            else {}
        )
        authorization_state = _optional_text(
            authorized_transit.get("status")
        )
        gripper = controller_state.get("gripper")
        gripper = gripper if isinstance(gripper, dict) else {}
        local_writer_active = bool(
            controller_state.get("engaged")
            or authorization_state
            in {
                "STARTING",
                "EXECUTING",
                "HOLDING_FINAL",
                "RELEASING",
            }
            or gripper.get("active_action")
        )
        with self.safe_termination_lock:
            relinquishment_state = str(
                self.safe_termination.get("state") or "IDLE"
            )
        try:
            manager_view = self.platform.control_authority(resource_id)
            evaluation = evaluate_authority_coordination(
                resource_id=resource_id,
                manager_available=True,
                manager_view=manager_view,
                local_basic_lease=local_lease,
                local_writer_active=local_writer_active,
                integrated_residency=controller_state.get("residency"),
                integrated_control_state=controller_state.get(
                    "control_state"
                ),
                motion_inhibited=self.motion_inhibited,
                authorization_state=authorization_state,
                relinquishment_state=relinquishment_state,
            )
            self._record_authority_evaluation(evaluation)
            self.manager_authority_status.update(
                {
                    "enforcement": str(manager_view.get("enforcement", "ADVISORY")),
                    "comparison": evaluation["state"],
                    "manager_view": manager_view,
                    "local_basic_lease": local_lease,
                    "last_error": None,
                    "evaluation": evaluation,
                }
            )
        except Exception as error:
            evaluation = evaluate_authority_coordination(
                resource_id=resource_id,
                manager_available=False,
                manager_view=None,
                local_basic_lease=local_lease,
                local_writer_active=local_writer_active,
                integrated_residency=controller_state.get("residency"),
                integrated_control_state=controller_state.get(
                    "control_state"
                ),
                motion_inhibited=self.motion_inhibited,
                authorization_state=authorization_state,
                relinquishment_state=relinquishment_state,
            )
            self._record_authority_evaluation(evaluation)
            self.manager_authority_status.update(
                {
                    "comparison": evaluation["state"],
                    "manager_view": None,
                    "local_basic_lease": local_lease,
                    "last_error": str(error),
                    "evaluation": evaluation,
                }
            )

    def _record_authority_evaluation(
        self,
        evaluation: dict[str, Any],
    ) -> None:
        metrics = self.manager_authority_status["metrics"]
        state = str(evaluation["state"])
        observed_at_us = int(evaluation["observed_at_us"])
        prior_state = metrics.get("last_state")
        metrics["poll_count"] = int(metrics.get("poll_count", 0)) + 1
        state_counts = metrics["state_counts"]
        state_counts[state] = int(state_counts.get(state, 0)) + 1
        disagreement_counts = metrics["disagreement_counts"]
        for reason in evaluation["disagreement_reasons"]:
            disagreement_counts[reason] = (
                int(disagreement_counts.get(reason, 0)) + 1
            )
        if prior_state != state:
            metrics["transition_count"] = (
                int(metrics.get("transition_count", 0)) + 1
            )
            metrics["last_transition_at_us"] = observed_at_us
        metrics["last_state"] = state

    def _audited_control(
        self,
        endpoint: str,
        body: dict[str, Any],
        operation,
        *,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        command_id = (
            str(command_id or body.get("command_id") or body.get("request_id") or "").strip()
            or str(uuid.uuid4())
        )
        related_skill_id = _optional_text(body.get("related_skill_id"))
        binding_id = _optional_text(body.get("binding_id"))
        authority_id = _optional_text(body.get("authority_id"))
        submitted = self.control_audit.record(
            lifecycle="SUBMITTED",
            endpoint=endpoint,
            command_id=command_id,
            canonical_request=body,
            related_skill_id=related_skill_id,
            binding_id=binding_id,
            authority_id=authority_id,
            plan_id=_optional_text(body.get("plan_id")),
        )
        try:
            result = operation()
        except Exception as operation_error:
            try:
                self.control_audit.record(
                    lifecycle="REJECTED",
                    endpoint=endpoint,
                    command_id=command_id,
                    canonical_request=body,
                    error=str(operation_error),
                    related_skill_id=related_skill_id,
                    binding_id=binding_id,
                    authority_id=authority_id,
                    plan_id=_optional_text(body.get("plan_id")),
                )
            except Exception:
                # The controlled operation already failed. Preserve that outcome
                # instead of replacing it with a secondary audit-write error.
                pass
            raise
        else:
            if not isinstance(result, dict):
                result = {"result": result}
            plan_id = _optional_text(
                result.get("plan_id")
                or result.get("preview_id")
                or (result.get("preview") or {}).get("preview_id")
            )
            accepted = None
            accepted_audit_error = None
            try:
                accepted = self.control_audit.record(
                    lifecycle="ACCEPTED",
                    endpoint=endpoint,
                    command_id=command_id,
                    canonical_request=body,
                    result=result,
                    related_skill_id=related_skill_id,
                    binding_id=binding_id,
                    authority_id=authority_id,
                    plan_id=plan_id,
                )
            except Exception as audit_error:
                # The operation has already completed. Report the audit failure
                # without changing or obscuring the controlled outcome.
                accepted_audit_error = str(audit_error)
            return {
                **result,
                "control_audit": {
                    "mode": self.control_audit.mode,
                    "command_id": command_id,
                    "submitted_event_id": submitted["audit_event_id"],
                    "submitted_local_persisted": bool(
                        (submitted.get("local_delivery") or {}).get("persisted")
                    ),
                    "accepted_event_id": (
                        accepted["audit_event_id"] if accepted is not None else None
                    ),
                    "accepted_local_persisted": bool(
                        ((accepted or {}).get("local_delivery") or {}).get("persisted")
                    ),
                    "post_action_audit_error": accepted_audit_error,
                    "plan_id": plan_id,
                },
            }

    def _direct_plan_motion(self, body: dict[str, Any]) -> dict[str, Any]:
        command = body.get("command", body)
        if not isinstance(command, dict):
            raise ValueError("command must be an object")
        staged = self.controller.stage_external_command(
            command,
            source="direct:/v1/motion/plan",
            metadata={
                "command_id": body.get("command_id"),
                "binding_id": body.get("binding_id"),
                "authority_id": body.get("authority_id"),
                "related_skill_id": body.get("related_skill_id"),
            },
        )
        allowed = body.get("allowed_contact_object_ids", [])
        if not isinstance(allowed, list):
            raise ValueError("allowed_contact_object_ids must be an array")
        preview = self.controller.preview_staged_target(
            allowed_contact_object_ids={str(value) for value in allowed},
            permit_pushable_contact=bool(body.get("permit_pushable_contact", False)),
        )
        return {
            "status": "PLANNED" if preview.get("planning_valid") else "REJECTED",
            "enforcement": "SHADOW_NONPHYSICAL",
            "physical_motion_authorized": False,
            "plan_id": preview.get("preview_id"),
            "normalized_target": staged.get("staged_target"),
            "runtime": staged.get("runtime"),
            "preview": preview,
        }

    def _direct_plan_transit_path(self, body: dict[str, Any]) -> dict[str, Any]:
        target = body.get("target")
        if not isinstance(target, dict):
            raise ValueError("target must be an object")
        position = target.get("position_m")
        if not isinstance(position, list):
            raise ValueError("target.position_m must be an array")
        orientation = target.get("rpy_rad")
        if orientation is not None and not isinstance(orientation, list):
            raise ValueError("target.rpy_rad must be an array when provided")
        allowed = body.get("allowed_contact_object_ids", [])
        if not isinstance(allowed, list):
            raise ValueError("allowed_contact_object_ids must be an array")
        request_context = body.get("request_context", {})
        if not isinstance(request_context, dict):
            raise ValueError("request_context must be an object")
        normalized_request = {
            "target": {
                "position_m": copy.deepcopy(position),
                "rpy_rad": copy.deepcopy(orientation),
            },
            "requested_speed_m_s": float(
                body.get("requested_speed_m_s", 0.05)
            ),
            "allowed_contact_object_ids": sorted(str(value) for value in allowed),
            "permit_pushable_contact": bool(
                body.get("permit_pushable_contact", False)
            ),
            "request_context": copy.deepcopy(request_context),
        }
        planning_result = self.controller.preview_transit_path(
            target_position_m=position,
            target_rpy_rad=orientation,
            requested_speed_m_s=normalized_request["requested_speed_m_s"],
            allowed_contact_object_ids={str(value) for value in allowed},
            permit_pushable_contact=normalized_request[
                "permit_pushable_contact"
            ],
        )
        issued_at_us = time.time_ns() // 1000
        ttl_ms = int(
            self.config.get("planning", {}).get(
                "transit_preview_ttl_ms",
                5000,
            )
        )
        ttl_ms = max(250, min(30_000, ttl_ms))
        required_context_fields = (
            "binding_id",
            "camera_provider_id",
            "camera_provider_instance_id",
            "camera_boot_id",
            "workcell_transform_id",
            "workcell_transform_revision",
            "workcell_transform_expires_at_us",
            "vio_session_epoch",
            "observation_timestamp_us",
            "observation_expires_at_us",
            "scene_revision",
        )
        context_issues = [
            f"MISSING_REQUEST_CONTEXT:{field}"
            for field in required_context_fields
            if field not in request_context
        ]
        for field in (
            "binding_id",
            "camera_provider_id",
            "camera_provider_instance_id",
            "camera_boot_id",
            "workcell_transform_id",
            "workcell_transform_revision",
            "vio_session_epoch",
            "scene_revision",
        ):
            if field in request_context and not str(
                request_context.get(field) or ""
            ).strip():
                context_issues.append(f"EMPTY_REQUEST_CONTEXT:{field}")
        for field in (
            "workcell_transform_expires_at_us",
            "observation_timestamp_us",
            "observation_expires_at_us",
        ):
            if field in request_context:
                try:
                    if int(request_context[field]) <= 0:
                        context_issues.append(
                            f"INVALID_REQUEST_CONTEXT:{field}"
                        )
                except (TypeError, ValueError):
                    context_issues.append(f"INVALID_REQUEST_CONTEXT:{field}")
        for field, issue in (
            (
                "workcell_transform_expires_at_us",
                "WORKCELL_TRANSFORM_EXPIRED",
            ),
            ("observation_expires_at_us", "OBSERVATION_EXPIRED"),
        ):
            try:
                if (
                    field in request_context
                    and int(request_context[field]) <= issued_at_us
                ):
                    context_issues.append(issue)
            except (TypeError, ValueError):
                pass
        expires_at_us = issued_at_us + ttl_ms * 1000
        for field in (
            "workcell_transform_expires_at_us",
            "observation_expires_at_us",
        ):
            try:
                expires_at_us = min(
                    expires_at_us,
                    int(request_context[field]),
                )
            except (KeyError, TypeError, ValueError):
                pass
        if (
            "scene_revision" in request_context
            and request_context.get("scene_revision")
            != planning_result.get("scene_revision")
        ):
            context_issues.append("SCENE_REVISION_MISMATCH")

        try:
            controller_state = self.controller.snapshot()
        except Exception:
            controller_state = {}
        lease_state = (
            controller_state.get("lease")
            if isinstance(controller_state, dict)
            else None
        )
        lease_state = lease_state if isinstance(lease_state, dict) else {}
        basic_state = (
            controller_state.get("basic_state")
            if isinstance(controller_state, dict)
            else None
        )
        basic_state = basic_state if isinstance(basic_state, dict) else {}
        contract = {
            "schema": "physical_agent.integrated_transit_preview_contract",
            "schema_version": 1,
            "preview_id": planning_result.get("plan_id"),
            "issued_at_us": issued_at_us,
            "expires_at_us": expires_at_us,
            "ttl_ms": max(
                0,
                (expires_at_us - issued_at_us) // 1000,
            ),
            "controller_provider_id": self.config["provider_id"],
            "controller_provider_instance_id": self.platform.instance_id,
            "controller_boot_id": self.platform.boot_id,
            "controller_configuration_sha256": _canonical_sha256(
                self.config
            ),
            "request_sha256": _canonical_sha256(normalized_request),
            "normalized_request": normalized_request,
            "request_context_sha256": _canonical_sha256(request_context),
            "request_context_complete": not context_issues,
            "request_context_issues": context_issues,
            "scene_revision": planning_result.get("scene_revision"),
            "lease_snapshot": {
                "active": bool(lease_state.get("active")),
                "state": lease_state.get("state"),
                "lease_id": lease_state.get("lease_id"),
                "fencing_generation": lease_state.get(
                    "fencing_generation"
                ),
            },
            "basic_feedback": {
                "observed_at_us": basic_state.get("observed_at_us"),
                "last_applied_command_id": basic_state.get(
                    "last_applied_command_id"
                ),
            },
            "physical_motion_authorized": False,
            "preview_grants_commit_authority": False,
            "commit_endpoint_exposed": False,
        }
        contract["preview_sha256"] = _canonical_sha256(
            {
                "planning_result": planning_result,
                "preview_contract": contract,
            }
        )
        result = {
            **planning_result,
            "preview_contract": contract,
        }
        plan_id = str(result.get("plan_id") or "").strip()
        if (
            plan_id
            and result.get("status") == "PLANNED"
            and contract["request_context_complete"]
        ):
            now_us = time.time_ns() // 1000
            with self.transit_plan_lock:
                self.transit_plans = {
                    key: value
                    for key, value in self.transit_plans.items()
                    if int(value["expires_at_us"]) > now_us
                    and value.get("state") != "CONSUMED"
                }
                self.transit_plans[plan_id] = {
                    "state": "PLANNED",
                    "created_at_us": issued_at_us,
                    "expires_at_us": contract["expires_at_us"],
                    "planning_result": copy.deepcopy(planning_result),
                    "preview_contract": copy.deepcopy(contract),
                    "normalized_request": copy.deepcopy(
                        normalized_request
                    ),
                }
        return result

    def _require_current_workcell_activation(
        self,
        request_context: dict[str, Any],
        *,
        now_us: int,
    ) -> dict[str, Any]:
        payload = self.platform.workcell_calibrations()
        activations = payload.get("activations")
        if not isinstance(activations, list):
            raise PermissionError(
                "Manager returned no workcell calibration activation list"
            )
        transform_id = str(
            request_context.get("workcell_transform_id") or ""
        )
        revision = str(
            request_context.get("workcell_transform_revision") or ""
        )
        matches = [
            item
            for item in activations
            if isinstance(item, dict)
            and (
                str(item.get("candidate_id") or "") == transform_id
                or str(item.get("activation_id") or "") == transform_id
            )
            and str(item.get("calibration_revision") or "") == revision
        ]
        if len(matches) != 1:
            raise PermissionError(
                "exact workcell calibration activation is not current"
            )
        activation = matches[0]
        expected_fields = {
            "camera_provider_id": "camera_provider_id",
            "camera_provider_instance_id": (
                "camera_provider_instance_id"
            ),
            "camera_boot_id": "camera_boot_id",
            "vio_session_epoch": "session_epoch",
        }
        identity_changed = any(
            str(activation.get(activation_field) or "")
            != str(request_context.get(context_field) or "")
            for context_field, activation_field in expected_fields.items()
        )
        try:
            activation_expires_at_us = int(
                activation.get("expires_at_us")
            )
            requested_expires_at_us = int(
                request_context.get(
                    "workcell_transform_expires_at_us"
                )
            )
        except (TypeError, ValueError) as exc:
            raise PermissionError(
                "workcell calibration activation expiry is invalid"
            ) from exc
        if (
            activation.get("state") != "ACTIVE"
            or activation.get("motion_usable") is not True
            or activation_expires_at_us <= now_us
            or requested_expires_at_us != activation_expires_at_us
            or identity_changed
        ):
            raise PermissionError(
                "workcell calibration activation was revoked, expired, "
                "or changed"
            )
        return activation

    def _direct_commit_transit_path(
        self,
        body: dict[str, Any],
        authorization_assertion: str,
    ) -> dict[str, Any]:
        plan_id = str(body.get("plan_id") or "").strip()
        request_sha256 = str(
            body.get("request_sha256") or ""
        ).strip()
        preview_sha256 = str(
            body.get("preview_sha256") or ""
        ).strip()
        if not plan_id or not request_sha256 or not preview_sha256:
            raise ValueError(
                "plan_id, request_sha256, and preview_sha256 are required"
            )
        if len(self.authorization_secret.encode("utf-8")) < 32:
            raise PermissionError(
                "signed physical authorization is not configured"
            )
        now_us = time.time_ns() // 1000
        with self.transit_plan_lock:
            plan = copy.deepcopy(self.transit_plans.get(plan_id))
        if plan is None:
            raise PermissionError(
                "transit plan is unavailable or already consumed"
            )
        if plan.get("state") != "PLANNED":
            raise PermissionError(
                f"transit plan state is {plan.get('state')}"
            )
        if int(plan["expires_at_us"]) <= now_us:
            raise PermissionError("transit plan has expired")
        contract = plan["preview_contract"]
        if contract.get("request_sha256") != request_sha256:
            raise PermissionError(
                "transit request digest does not match the stored preview"
            )
        if contract.get("preview_sha256") != preview_sha256:
            raise PermissionError(
                "transit preview digest does not match the stored preview"
            )
        normalized_request = plan["normalized_request"]
        self._require_current_workcell_activation(
            normalized_request["request_context"],
            now_us=now_us,
        )
        selected_plan = plan["planning_result"].get("selected_plan")
        selected_plan = (
            selected_plan if isinstance(selected_plan, dict) else {}
        )
        q_waypoints = selected_plan.get("q_waypoints_rad")
        if not isinstance(q_waypoints, list):
            raise RuntimeError(
                "stored transit preview has no executable joint waypoints"
            )

        claims = verify_transit_execution_assertion(
            authorization_assertion,
            self.authorization_secret,
            provider_id=self.config["provider_id"],
            provider_instance_id=self.platform.instance_id,
            boot_id=self.platform.boot_id,
            configuration_sha256=_canonical_sha256(self.config),
            plan_id=plan_id,
            request_sha256=request_sha256,
            preview_sha256=preview_sha256,
            scene_revision=contract.get("scene_revision"),
            preview_expires_at_us=int(contract["expires_at_us"]),
            now_us=now_us,
        )
        assertion_id = str(claims["assertion_id"])
        with self.transit_plan_lock:
            current = self.transit_plans.get(plan_id)
            if current is None or current.get("state") != "PLANNED":
                raise PermissionError(
                    "transit plan was consumed by another request"
                )
            if (
                assertion_id
                in self.consumed_authorization_assertion_ids
            ):
                raise PermissionError(
                    "authorization assertion was already consumed"
                )
            self.consumed_authorization_assertion_ids.add(
                assertion_id
            )
            current["state"] = "COMMITTING"
            current["assertion_id"] = assertion_id
            current["decision_id"] = claims["decision_id"]

        try:
            result = self.controller.execute_authorized_transit(
                plan_id=plan_id,
                preview_sha256=preview_sha256,
                request_sha256=request_sha256,
                q_waypoints_rad=q_waypoints,
                requested_speed_m_s=float(
                    normalized_request["requested_speed_m_s"]
                ),
                scene_revision=str(contract["scene_revision"]),
                allowed_contact_object_ids={
                    str(value)
                    for value in normalized_request[
                        "allowed_contact_object_ids"
                    ]
                },
                permit_pushable_contact=bool(
                    normalized_request[
                        "permit_pushable_contact"
                    ]
                ),
                authorization_claims=claims,
            )
        except Exception:
            with self.transit_plan_lock:
                current = self.transit_plans.get(plan_id)
                if current is not None:
                    current["state"] = "REJECTED_AT_COMMIT"
            raise
        with self.transit_plan_lock:
            current = self.transit_plans.get(plan_id)
            if current is not None:
                current["state"] = "CONSUMED"
                current["committed_at_us"] = time.time_ns() // 1000
        return {
            **result,
            "authorization": {
                "assertion_id": assertion_id,
                "decision_id": claims["decision_id"],
                "resolved_by": claims["resolved_by"],
                "assertion_sha256": hashlib.sha256(
                    authorization_assertion.encode("utf-8")
                ).hexdigest(),
                "one_time": True,
            },
        }

    def _direct_release_transit_path(self) -> dict[str, Any]:
        return self.controller.release_authorized_transit()

    def _teleop_result(self, body: dict[str, Any]) -> dict[str, Any]:
        self.controller.update_input(body)
        state = self.controller.snapshot()
        return {
            "accepted": True,
            "normalized_target": copy.deepcopy(state.get("target")),
            "physical_motion_authorized": bool(
                state.get("engaged") and body.get("lb")
            ),
        }

    def _request_service_stop(self) -> dict[str, str]:
        timer = threading.Timer(0.15, self.shutdown)
        timer.daemon = True
        timer.start()
        return {"status": "stopping_float_then_release"}

    def start_safe_termination(self) -> dict[str, Any]:
        with self.safe_termination_lock:
            if self.safe_termination["state"] not in {"IDLE", "FAILED"}:
                return copy.deepcopy(self.safe_termination)
            self.safe_termination = {
                "state": "STARTING",
                "message": "Safe termination requested",
                "started_at_monotonic": time.monotonic(),
                "manager_shadow_plan": None,
            }

        threading.Thread(
            target=self._request_manager_shutdown_shadow,
            name="manager-shutdown-shadow",
            daemon=True,
        ).start()

        if os.name == "nt":
            project_root = self.provider_root.parents[1].resolve()
            helper = self.provider_root / "scripts" / "safe_terminate_detached.ps1"
            if not helper.exists():
                self._set_safe_termination("FAILED", f"Safe termination helper is missing: {helper}")
                return {"status": "failed", "safe_termination": copy.deepcopy(self.safe_termination)}
            log_path = self.provider_root / "runtime_logs" / "safe_terminate.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            launch_id = f"{os.getpid()}-{time.time_ns()}"
            with log_path.open("a", encoding="utf-8") as log:
                log.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} GUI launch requested. "
                    f"launch_id={launch_id}\n"
                )
            powershell = (
                Path(os.environ.get("SystemRoot", r"C:\Windows"))
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            if not powershell.exists():
                self._set_safe_termination(
                    "FAILED", f"Windows PowerShell executable is missing: {powershell}"
                )
                return {
                    "status": "failed",
                    "safe_termination": copy.deepcopy(self.safe_termination),
                }
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
            try:
                process = subprocess.Popen(
                    [
                        str(powershell),
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(helper),
                        "-ProjectRoot",
                        str(project_root),
                        "-BasicUrl",
                        str(self.config["basic_controller_url"]),
                        "-IntegratedUrl",
                        self.control_url,
                        "-LaunchId",
                        launch_id,
                    ],
                    cwd=str(project_root),
                    creationflags=creationflags,
                    startupinfo=startupinfo,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            except OSError as error:
                self._set_safe_termination(
                    "FAILED",
                    f"Safe termination helper failed to launch: {error}",
                )
                return {
                    "status": "failed",
                    "safe_termination": copy.deepcopy(self.safe_termination),
                }
            acknowledgement = f"Authoritative safe termination started. launch_id={launch_id}"
            acknowledged = False
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    if acknowledgement in log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ):
                        acknowledged = True
                        break
                except OSError:
                    pass
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            with self.safe_termination_lock:
                self.safe_termination["state"] = (
                    "RUNNING" if acknowledged else "LAUNCH_UNCONFIRMED"
                )
                self.safe_termination["message"] = (
                    "Authoritative shutdown helper acknowledged; safe-home is running"
                    if acknowledged
                    else (
                        "Shutdown helper did not acknowledge startup; use the official "
                        "terminal command and inspect the log"
                    )
                )
                self.safe_termination["log_path"] = str(log_path)
                self.safe_termination["launch_id"] = launch_id
                self.safe_termination["process_id"] = process.pid
            return {
                "status": "accepted" if acknowledged else "unconfirmed",
                "safe_termination": copy.deepcopy(self.safe_termination),
            }

        thread = threading.Thread(target=self._safe_termination_worker, name="safe-termination", daemon=True)
        thread.start()
        return {"status": "accepted", "safe_termination": copy.deepcopy(self.safe_termination)}

    def _request_manager_shutdown_shadow(self) -> None:
        owner_id = f"{self.config['provider_id']}:{self.platform.instance_id}"
        try:
            manager_shadow_plan = self.platform.shutdown_plan(
                owner_id,
                "Integrated controller safe termination requested",
            )
        except Exception as error:
            manager_shadow_plan = {
                "state": "UNAVAILABLE",
                "enforcement": "SHADOW_DRY_RUN",
                "error": str(error),
            }
        with self.safe_termination_lock:
            self.safe_termination["manager_shadow_plan"] = manager_shadow_plan

    def _set_safe_termination(self, state: str, message: str) -> None:
        with self.safe_termination_lock:
            self.safe_termination["state"] = state
            self.safe_termination["message"] = message

    def _safe_termination_worker(self) -> None:
        try:
            self._set_safe_termination("RELEASING_LEASE", "Floating and releasing Integrated control lease")
            self.controller.enter_warm()
            if self.controller.basic.lease_snapshot() is not None:
                raise RuntimeError("Integrated WARM did not release the Basic lease")

            self._set_safe_termination("SAFE_HOMING", "Basic Controller is executing safe-home")
            self.controller.basic.safe_home_stop()
            deadline = time.monotonic() + 45.0
            while time.monotonic() < deadline:
                try:
                    self.controller.basic.health()
                except Exception:
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    "Basic is still running after the safe-home window; core was NOT stopped so gravity support can remain active"
                )

            self._set_safe_termination("ARM_SAFE", "Basic safe-home completed; stopping Midbrain workspace")
            project_root = Path.cwd()
            stop_script = project_root / "platform_core" / "scripts" / "stop_workspace.ps1"
            if os.name == "nt" and stop_script.exists():
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                subprocess.Popen(
                    ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(stop_script)],
                    cwd=str(project_root),
                    creationflags=creationflags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._set_safe_termination("STOPPING_CORE", "Safe-home complete; workspace stop launched")
            else:
                self._set_safe_termination("ARM_SAFE", "Safe-home complete; stop Midbrain core manually")
                timer = threading.Timer(0.5, self.shutdown)
                timer.daemon = True
                timer.start()
        except Exception as exc:
            self._set_safe_termination("FAILED", str(exc))

    def shutdown(self) -> None:
        if self.shutdown_event.is_set():
            return
        self.controller.stop()
        self.shutdown_event.set()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def _manager_request(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action", "")).strip()
        request_id = str(body.get("request_id") or "").strip()
        if not action:
            raise ValueError("action is required")
        if request_id:
            with self.request_lock:
                cached = self.request_results.get(request_id)
                if cached is not None:
                    return copy.deepcopy(cached)

        if action in {"gravity_float", "disengage"}:
            result = self.controller.request_float()
        elif action == "warm":
            result = self.controller.enter_warm()
        elif action == "hot":
            result = self.controller.enter_hot()
        else:
            raise ValueError(
                f"unsupported action {action}; Manager requests cannot authorize motion"
            )

        result = {**result, "request_id": request_id or None, "idempotent": bool(request_id)}
        if request_id:
            with self.request_lock:
                self.request_results[request_id] = copy.deepcopy(result)
                while len(self.request_results) > 128:
                    self.request_results.pop(next(iter(self.request_results)))
        return result

    def capability_catalog(self) -> dict[str, Any]:
        state = self.controller.snapshot()
        profiles = state.get("capability_profiles", {})
        readiness = state.get("capability_readiness", {})
        capabilities = []
        for capability, available in readiness.items():
            capabilities.append(
                {
                    "capability": capability,
                    "available": bool(available),
                    **copy.deepcopy(profiles.get(capability, {})),
                }
            )
        return {
            "schema": "physical_agent.provider_capability_catalog",
            "schema_version": 1,
            "provider_id": self.config["provider_id"],
            "manager_catalog_source": "heartbeat.details.capability_readiness",
            "capabilities": capabilities,
            "upstream_operations": {
                "state": {"method": "GET", "path": "/v1/state"},
                "engage": {
                    "method": "POST",
                    "path": "/v1/engage",
                    "caller_policy": "OPERATOR_OR_OPERATOR_SUPERVISED_SKILL",
                },
                "teleop_input": {
                    "method": "POST",
                    "path": "/v1/teleop",
                    "caller_policy": "OPERATOR_OR_OPERATOR_SUPERVISED_SKILL",
                },
                "settings": {"method": "POST", "path": "/v1/settings"},
                "gripper_settings": {"method": "POST", "path": "/v1/gripper/settings"},
                "gripper_action": {"method": "POST", "path": "/v1/gripper"},
                "nonphysical_preview": {"method": "POST", "path": "/v1/preview"},
                "direct_motion_plan": {
                    "method": "POST",
                    "path": "/v1/motion/plan",
                    "physical_motion_authorized": False,
                    "fabric_in_synchronous_path": False,
                },
                "controller_transit_path_shadow": {
                    "method": "POST",
                    "path": "/v1/motion/path-plan",
                    "planner_owner": "ROBOT_ARM_INTEGRATED_CONTROLLER",
                    "physical_motion_authorized": False,
                    "may_switch_control_mode": False,
                    "fabric_in_synchronous_path": False,
                },
                "authorized_staged_transit_commit": {
                    "method": "POST",
                    "path": "/v1/motion/path-commit",
                    "planner_owner": (
                        "ROBOT_ARM_INTEGRATED_CONTROLLER"
                    ),
                    "authorization": (
                        "SIGNED_SHORT_LIVED_ONE_TIME_ASSERTION"
                    ),
                    "exact_preview_digest_required": True,
                    "fabric_in_synchronous_path": False,
                    "local_control_audit_required": True,
                    "fabric_audit_copy": True,
                },
                "authorized_staged_transit_release": {
                    "method": "POST",
                    "path": "/v1/motion/path-release",
                    "behavior": "EXPLICIT_GRAVITY_FLOAT",
                },
                "contact_baseline_capture": {"method": "POST", "path": "/v1/contact-baseline"},
                "semantic_scene_staging": {"method": "POST", "path": "/v1/scene"},
                "gravity_float": {"method": "POST", "path": "/v1/float"},
                "safe_terminate": {"method": "POST", "path": "/v1/safe-terminate"},
                "manager_authority_observation": {
                    "mode": self.manager_authority_status.get("mode"),
                    "resource_id": self.manager_authority_status.get("resource_id"),
                    "enforcement": "ADVISORY",
                    "physical_enforcement": False,
                    "may_replace_local_basic_lease": False,
                    "may_switch_control_mode": False,
                    "may_submit_motor_commands": False,
                },
                "cartesian_target_staging": {
                    "transport": "FABRIC",
                    "stream": self.config.get("fabric_input", {}).get("stream"),
                    "schema": self.config.get("fabric_input", {}).get("schema"),
                },
            },
            "physical_execution_gate": {
                "authority": "OPERATOR_OR_SIGNED_UI_DECISION",
                "required": [
                    "PATH_SPECIFIC_GATE",
                    "FRESH_LOCAL_BASIC_LEASE",
                ],
                "upstream_motion_authority": False,
                "operator_debug_path": {
                    "authority": "OPERATOR",
                    "required": ["GUI_ENGAGE", "XBOX_LB"],
                },
                "agentic_transit_path": {
                    "authority": "SIGNED_UI_DECISION",
                    "required": [
                        "EXACT_CONTROLLER_PREVIEW",
                        "ONE_TIME_AUTHORIZATION_ASSERTION",
                        "FRESH_LOCAL_BASIC_LEASE",
                        "CURRENT_SEMANTIC_SCENE",
                    ],
                    "configured": bool(
                        len(
                            self.authorization_secret.encode("utf-8")
                        )
                        >= 32
                    ),
                },
            },
            "control_audit": self.control_audit.status(),
            "manager_authority": copy.deepcopy(self.manager_authority_status),
            "non_discoverable_experiments": copy.deepcopy(
                state.get("non_discoverable_experiments", {})
            ),
        }

    @staticmethod
    def _error_payload(exc: Exception, code: str) -> dict[str, Any]:
        return {
            "error": str(exc),
            "error_code": code,
            "severity": "ERROR",
            "retry_recommendation": "RETRY_AFTER_STATE_CHANGE",
            "safety_impact": "MOTION_BLOCKED_OR_FLOAT_REQUESTED",
            "physical_outcome_known": False,
            "affected_functions": ["robot.motion.arm.integrated"],
        }

    def _handler(self):
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ArmIntegratedMIT/0.8.0"

            def log_message(self, fmt, *args):
                try:
                    status = int(args[1]) if len(args) > 1 else 200
                except (TypeError, ValueError):
                    status = 200
                if status >= 400:
                    print(f"[staged-http] {self.path} {fmt % args}")

            def _write_bytes(self, data: bytes) -> bool:
                try:
                    self.wfile.write(data)
                    return True
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return False

            def _json(self, status: int, payload: Any):
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                self._write_bytes(data)
                return None

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                return {} if not length else json.loads(self.rfile.read(length))

            def _static(self, path: str):
                relative = "index.html" if path in {"/", ""} else path.lstrip("/")
                target = (service.web_root / relative).resolve()
                root = service.web_root.resolve()
                if root not in target.parents and target != root:
                    return self._json(404, {"error": "not found"})
                if not target.exists() or not target.is_file():
                    return self._json(404, {"error": "not found"})
                data = target.read_bytes()
                try:
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        mimetypes.guess_type(str(target))[0] or "application/octet-stream",
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                self._write_bytes(data)
                return None

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header(
                    "Access-Control-Allow-Headers",
                    (
                        "Content-Type, X-Midbrain-Command-ID, "
                        "X-Midbrain-Authorization"
                    ),
                )
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
                self.end_headers()

            def do_GET(self):
                try:
                    parsed_path = urlparse(self.path)
                    request_path = parsed_path.path
                    if request_path == "/health":
                        return self._json(
                            200,
                            {
                                **service._state_payload(),
                                "platform_errors": service.platform.errors(),
                                "manager_registered": service.manager_registered,
                                "fabric_ready": service.fabric_ready,
                                "motion_inhibited": service.motion_inhibited,
                            },
                        )
                    if request_path == "/v1/state":
                        return self._json(200, service._state_payload())
                    if request_path == "/v1/config":
                        return self._json(200, service.config)
                    if request_path == "/v1/capabilities":
                        return self._json(200, service.capability_catalog())
                    if request_path == "/v1/control-audit":
                        query = parse_qs(parsed_path.query)
                        limit = int((query.get("limit") or ["50"])[0])
                        return self._json(
                            200,
                            service.control_audit.recent_events(limit=limit),
                        )
                    if request_path == "/favicon.ico":
                        self.send_response(204)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return None
                    return self._static(request_path)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                except Exception as exc:
                    return self._json(500, service._error_payload(exc, "READ_FAILED"))

            def do_POST(self):
                try:
                    body = self._body()
                    if self.path == "/v1/control/hot":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                service.controller.enter_hot,
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/control/warm":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                service.controller.enter_warm,
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/control/stop":
                        return self._json(
                            202,
                            service._audited_control(
                                self.path,
                                body,
                                service._request_service_stop,
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/control/request":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service._manager_request(body),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/motion/plan":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service._direct_plan_motion(body),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/motion/path-plan":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service._direct_plan_transit_path(body),
                                command_id=self.headers.get(
                                    "X-Midbrain-Command-ID"
                                ),
                            ),
                        )
                    if self.path == "/v1/motion/path-commit":
                        assertion = str(
                            self.headers.get(
                                "X-Midbrain-Authorization",
                                "",
                            )
                        ).strip()
                        audited_body = copy.deepcopy(body)
                        audited_body[
                            "authorization_assertion_sha256"
                        ] = hashlib.sha256(
                            assertion.encode("utf-8")
                        ).hexdigest()
                        return self._json(
                            202,
                            service._audited_control(
                                self.path,
                                audited_body,
                                lambda: (
                                    service._direct_commit_transit_path(
                                        body,
                                        assertion,
                                    )
                                ),
                                command_id=self.headers.get(
                                    "X-Midbrain-Command-ID"
                                ),
                            ),
                        )
                    if self.path == "/v1/motion/path-release":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                service._direct_release_transit_path,
                                command_id=self.headers.get(
                                    "X-Midbrain-Command-ID"
                                ),
                            ),
                        )
                    if self.path == "/v1/teleop":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service._teleop_result(body),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/engage":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service.controller.set_engaged(
                                    bool(body.get("enabled", False))
                                ),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/settings":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service.controller.set_runtime_settings(body),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/gripper/settings":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service.controller.set_gripper_settings(body),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/gripper":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service.controller.request_gripper(
                                    str(body.get("action", ""))
                                ),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/preview":
                        allowed = body.get("allowed_contact_object_ids", [])
                        if not isinstance(allowed, list):
                            raise ValueError("allowed_contact_object_ids must be an array")
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service.controller.preview_staged_target(
                                    allowed_contact_object_ids={
                                        str(value) for value in allowed
                                    },
                                    permit_pushable_contact=bool(
                                        body.get("permit_pushable_contact", False)
                                    ),
                                ),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/contact-baseline":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                service.controller.capture_contact_baseline,
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/scene":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                lambda: service.controller.stage_scene(
                                    body,
                                    source="operator-api",
                                ),
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/float":
                        return self._json(
                            200,
                            service._audited_control(
                                self.path,
                                body,
                                service.controller.request_float,
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    if self.path == "/v1/safe-terminate":
                        return self._json(
                            202,
                            service._audited_control(
                                self.path,
                                body,
                                service.start_safe_termination,
                                command_id=self.headers.get("X-Midbrain-Command-ID"),
                            ),
                        )
                    return self._json(404, {"error": "not found"})
                except PermissionError as exc:
                    return self._json(403, service._error_payload(exc, "MOTION_NOT_AUTHORIZED"))
                except (ValueError, RuntimeError) as exc:
                    return self._json(409, service._error_payload(exc, "STATE_CONFLICT"))
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    return None
                except Exception as exc:
                    return self._json(500, service._error_payload(exc, "INTERNAL_ERROR"))

        return Handler
