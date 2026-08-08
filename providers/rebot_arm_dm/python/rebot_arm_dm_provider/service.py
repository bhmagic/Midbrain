"""Provider HTTP service, platform publication, and calibration experiments."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import json
import threading
import time
import uuid

import numpy as np

from .calibration import SessionRecorder, fit_two_parameter_friction
from .controller import ArmController, CommandEnvelope, JointCommand, ProviderState, LeasePermissionError
from .fabric import PlatformPublisher
from .kinematics import RebotKinematics
from .models import ArmConfiguration


class ArmProviderService:
    def __init__(self, configuration: ArmConfiguration, controller: ArmController, kinematics: RebotKinematics,
                 calibration_path: str|Path, listen_host: str, listen_port: int,
                 manager_url: str|None = None, fabric_url: str|None = None,
                 allow_hardware_calibration: bool = False, simulation: bool = False,
                 read_only: bool = False):
        self.configuration=configuration; self.controller=controller; self.kinematics=kinematics
        self.calibration_path=Path(calibration_path); self.listen_host=listen_host; self.listen_port=listen_port
        self.control_url=f"http://{listen_host}:{listen_port}"; self.instance_id=str(uuid.uuid4()); self.boot_id=str(uuid.uuid4())
        self.publisher=PlatformPublisher("robot_arm.rebot_dm",self.instance_id,self.boot_id,manager_url,fabric_url)
        self.allow_hardware_calibration=allow_hardware_calibration; self.simulation=simulation
        self.read_only=read_only
        self.shutdown_event=threading.Event(); self.httpd:ThreadingHTTPServer|None=None
        self.platform_threads:list[threading.Thread]=[]; self.model_published=False
        self.manager_registered=False
        self.motion_inhibited=False
        self.motion_inhibit_owners=[]
        self.platform_safety_block_active=False
        self.manager_request_results: dict[str, dict[str, Any]] = {}
        self.manager_request_lock = threading.Lock()
        self.recorder=SessionRecorder(self.calibration_path.parent/'sessions')
        self.experiment_lock=threading.Lock(); self.experiment_cancel_event=threading.Event()

    def start(self) -> None:
        self.controller.start()
        # Physical startup is gravity-supported by default. Calibration
        # permission is independent from whether the arm receives safe support.
        if not self.read_only and self.controller.state == ProviderState.READ_ONLY:
            self.controller.enable()
        handler = self._handler_type()
        try:
            self.httpd = ThreadingHTTPServer((self.listen_host, self.listen_port), handler)
            self.httpd.daemon_threads = True
            threading.Thread(
                target=self.httpd.serve_forever,
                name="rebot-arm-http",
                daemon=True,
            ).start()
        except Exception:
            self.controller.close(force=True)
            raise
        self.platform_threads = [
            threading.Thread(
                target=self._registration_loop,
                name="rebot-arm-manager-heartbeat",
                daemon=True,
            ),
            threading.Thread(
                target=self._motion_inhibit_loop,
                name="rebot-arm-motion-inhibit",
                daemon=True,
            ),
            threading.Thread(
                target=self._joint_publish_loop,
                name="rebot-arm-joint-publish",
                daemon=True,
            ),
            threading.Thread(
                target=self._transform_publish_loop,
                name="rebot-arm-transform-publish",
                daemon=True,
            ),
        ]
        for thread in self.platform_threads:
            thread.start()

    def wait(self) -> None:
        while not self.shutdown_event.wait(0.2): pass

    def disarm_to_float(self, reason: str = "operator interrupt") -> None:
        """Fence active control and retain powered high-kp gravity support."""
        if self.controller.state in {ProviderState.DISCONNECTED, ProviderState.EMERGENCY_DISABLED}:
            return
        if self.controller.state == ProviderState.READ_ONLY:
            if self.read_only:
                return
            self.controller.enable()
        self.controller.revoke_lease(reason)

    def shutdown(self, graceful: bool=True) -> bool:
        result=self.controller.close(force=not graceful)
        if not result and graceful:
            return False
        self.shutdown_event.set()
        if self.httpd:
            self.httpd.shutdown(); self.httpd.server_close()
        return True

    def _platform_state(self) -> dict[str, Any]:
        state = self.controller.snapshot()
        controller_ready = bool(
            state.get("ready", True)
            and state.get("provider_state", state.get("state"))
            not in {"DISCONNECTED", "FAULTED", "EMERGENCY_DISABLED"}
        )
        motion_allowed = bool(
            controller_ready and self.manager_registered and not self.motion_inhibited
        )
        joint_output = self.publisher.output_status("robot_arm.joint_state")
        transform_output = self.publisher.output_status("robot_arm.transforms.local")
        joint_output_ready = bool(
            controller_ready
            and isinstance(joint_output["age_ms"], (int, float))
            and float(joint_output["age_ms"]) <= 200.0
        )
        transform_output_ready = bool(
            controller_ready
            and isinstance(transform_output["age_ms"], (int, float))
            and float(transform_output["age_ms"]) <= 200.0
        )
        output_timestamps = [
            int(value)
            for value in (
                joint_output["observed_at_us"],
                transform_output["observed_at_us"],
            )
            if isinstance(value, int)
        ]
        lease = state.get("lease")
        state.update(
            {
                "manager_connected": self.manager_registered,
                "fabric_connected": self.publisher.fabric_url is not None
                and self.publisher.fabric_error is None,
                "motion_inhibited": self.motion_inhibited,
                "motion_inhibit_owners": list(self.motion_inhibit_owners),
                "midbrain_motion_allowed": motion_allowed,
                "capability_readiness": {
                    "robot.motion.arm.basic": motion_allowed,
                    "robot_arm.joint_state": joint_output_ready,
                    "robot_arm.transforms.local": transform_output_ready,
                    "robot_arm.gravity_float": controller_ready,
                    "robot_arm.control.impedance": motion_allowed,
                },
                "publication_outputs": {
                    "robot_arm.joint_state": joint_output,
                    "robot_arm.transforms.local": transform_output,
                },
                "held_control_authority_leases": [],
                "provider_local_control_lease": lease,
                "manager_authority_lease_supported": False,
                "midbrain_contract_status": "RUNTIME_ALIGNED_AUTHORITY_API_PENDING",
                "audited_midbrain_commit": "e226a09",
                "last_successful_output_timestamp_us": (
                    max(output_timestamps) if output_timestamps else None
                ),
            }
        )
        return state

    def _assert_operational_authority(self) -> None:
        if not self.manager_registered:
            raise PermissionError("Midbrain Manager is not registered; physical motion is blocked")
        if self.motion_inhibited:
            raise PermissionError("Midbrain global motion inhibit is active")

    def _enforce_platform_safety(self) -> None:
        blocked = bool(
            self.motion_inhibited
            or (
                self.publisher.manager_url is not None
                and not self.manager_registered
            )
        )
        if blocked and not self.platform_safety_block_active:
            reason = (
                "Midbrain global motion inhibit"
                if self.motion_inhibited
                else "Midbrain Manager connection lost"
            )
            self.disarm_to_float(reason)
        self.platform_safety_block_active = blocked

    def renew_operational_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._assert_operational_authority()
        return self.renew_lease(payload)

    def operational_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._assert_operational_authority()
        return self.command(payload)

    def operational_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._assert_operational_authority()
        value = self.controller.set_payload(
            str(payload["lease_id"]),
            int(payload["fencing_generation"]),
            float(payload.get("mass_kg", 0.0)),
            payload.get("com_tool_m", [0.0, 0.0, 0.0]),
        )
        return {"status": "payload_updated", "payload": value}

    def _registration_loop(self) -> None:
        next_register = 0.0
        next_heartbeat = 0.0
        while not self.shutdown_event.is_set():
            now = time.monotonic()
            if not self.manager_registered and now >= next_register:
                try:
                    self.publisher.register(self._platform_state(), self.control_url)
                    self.manager_registered = self.publisher.manager_url is not None
                except Exception as exc:
                    print(f"[basic-platform] Manager registration deferred: {exc}")
                next_register = now + 2.0
            if now >= next_heartbeat:
                try:
                    self.publisher.heartbeat(self._platform_state(), self.control_url)
                    self.manager_registered = self.publisher.manager_url is not None
                except Exception:
                    self.manager_registered = False
                next_heartbeat = now + 1.0
            self.shutdown_event.wait(0.05)

    def _motion_inhibit_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                inhibit = self.publisher.motion_inhibit()
                self.motion_inhibited = bool(inhibit.get("inhibited", False))
                owners = inhibit.get("owners", [])
                self.motion_inhibit_owners = owners if isinstance(owners, list) else []
            except Exception:
                self.manager_registered = False
            self._enforce_platform_safety()
            self.shutdown_event.wait(0.2)

    def _joint_publish_loop(self) -> None:
        rate = float(self.configuration.model["control"]["fabric_rate_hz"])
        period = max(0.002, 1.0 / rate)
        next_model = 0.0
        while not self.shutdown_event.is_set():
            snapshot = self.controller.snapshot()
            now = time.monotonic()
            if now >= next_model:
                try:
                    self.publisher.publish(
                        "robot_arm.model",
                        "physical_agent.robot_arm_model",
                        self.configuration.public_model(),
                        self.configuration.model["frames"]["base"],
                        self.configuration.calibration_revision,
                        5000,
                    )
                except Exception:
                    pass
                next_model = now + 2.0
            if "positions_rad" in snapshot:
                try:
                    self.publisher.publish(
                        "robot_arm.joint_state",
                        "physical_agent.robot_arm_joint_state",
                        snapshot,
                        self.configuration.model["frames"]["base"],
                        self.configuration.calibration_revision,
                        200,
                        int(snapshot["observed_at_us"]),
                    )
                except Exception:
                    pass
            self.shutdown_event.wait(period)

    def _transform_publish_loop(self) -> None:
        period = 1.0 / max(
            float(self.configuration.model["control"].get("transform_rate_hz", 30.0)),
            1.0,
        )
        while not self.shutdown_event.is_set():
            snapshot = self.controller.snapshot()
            if "positions_rad" in snapshot:
                try:
                    observed_at = int(snapshot["observed_at_us"])
                    observations = []
                    for transform in self.kinematics.public_transforms(snapshot["positions_rad"]):
                        transform["authority"] = "robot_arm.rebot_dm"
                        transform["session_epoch"] = self.boot_id
                        stream = (
                            f"transform.robot_arm.{transform['child_frame']}"
                            f"_from_{transform['parent_frame']}"
                        )
                        observations.append(
                            self.publisher.observation(
                                stream,
                                "physical_agent.transform",
                                transform,
                                transform["parent_frame"],
                                self.configuration.calibration_revision,
                                200,
                                observed_at,
                            )
                        )
                    self.publisher.publish_batch(
                        observations,
                        success_key="robot_arm.transforms.local",
                    )
                except Exception:
                    pass
            self.shutdown_event.wait(period)

    def handle_manager_request(self, body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action", "")).strip()
        request_id = str(body.get("request_id") or "").strip()
        if not action:
            raise ValueError("action is required")
        if request_id:
            with self.manager_request_lock:
                cached = self.manager_request_results.get(request_id)
                if cached is not None:
                    return dict(cached)
        request_payload = body.get("payload", {})
        if not isinstance(request_payload, dict):
            request_payload = {}
        if action == "gravity_float":
            self.controller.request_gravity_float(
                str(request_payload.get("reason", "manager request"))
            )
            result = {"status": "gravity_float"}
        elif action == "safe_home":
            max_velocity_rad_s = request_payload.get("max_velocity_rad_s")
            success = self.controller.safe_home(
                max_velocity_rad_s=(
                    None
                    if max_velocity_rad_s is None
                    else float(max_velocity_rad_s)
                )
            )
            result = {
                "status": "safe_home",
                "success": success,
                "details": self.controller.snapshot().get("last_safe_home_result"),
            }
        elif action == "revoke_lease":
            reason = str(
                request_payload.get(
                    "reason", body.get("reason", "manager lease handover")
                )
            )
            self.controller.revoke_lease(reason)
            result = {"status": "lease_revoked_gravity_float"}
        elif action == "emergency_disable":
            self.controller.emergency_disable(
                str(request_payload.get("reason", "manager emergency request"))
            )
            result = {"status": "emergency_disabled"}
        else:
            raise ValueError(f"unsupported action {action}")
        result = {
            **result,
            "request_id": request_id or None,
            "idempotent": bool(request_id),
        }
        if request_id:
            with self.manager_request_lock:
                self.manager_request_results[request_id] = dict(result)
                while len(self.manager_request_results) > 128:
                    self.manager_request_results.pop(
                        next(iter(self.manager_request_results))
                    )
        return result

    def health(self) -> dict[str, Any]:
        return {
            "provider_id": "robot_arm.rebot_dm",
            "instance_id": self.instance_id,
            "boot_id": self.boot_id,
            "simulation": self.simulation,
            "allow_hardware_calibration": self.allow_hardware_calibration,
            "read_only": self.read_only,
            "manager_registered": self.manager_registered,
            "motion_inhibited": self.motion_inhibited,
            "motion_inhibit_owners": list(self.motion_inhibit_owners),
            "midbrain_contract_status": "RUNTIME_ALIGNED_AUTHORITY_API_PENDING",
            "audited_midbrain_commit": "e226a09",
            "controller": self.controller.snapshot(),
            "platform_errors": self.publisher.errors(),
            "platform_publish_error": self.publisher.last_error,
            "operational_control_api_version": 2,
        }


    def acquire_operational_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._assert_operational_authority()
        if self.read_only:
            raise PermissionError("control leases are blocked in explicit read-only mode")
        if self.controller.state == ProviderState.DISCONNECTED:
            self.controller.start()
        if self.controller.state == ProviderState.READ_ONLY:
            self.controller.enable()
        # Acquire ownership before changing the motor state. This prevents a
        # rejected contender from disturbing the current lease holder.
        lease = self.controller.acquire_lease(
            str(payload.get("holder", "runtime_controller")),
            int(payload.get("duration_ms", self.configuration.model["control"]["lease_timeout_ms"])),
        )
        self.controller.request_gravity_float("operational lease acquisition")
        return {
            "lease_id": lease.lease_id,
            "fencing_generation": lease.fencing_generation,
            "expires_in_ms": int((lease.expires_monotonic-time.monotonic())*1000),
        }

    def release_operational_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.controller.release_lease(
            str(payload["lease_id"]),
            int(payload["fencing_generation"]),
            fallback_to_float=True,
        )
        return {"status": "released_gravity_float"}

    def acquire_lease(self,payload:dict[str,Any]) -> dict[str,Any]:
        if self.read_only:
            raise PermissionError("control leases are blocked in explicit read-only mode")
        if not self.simulation and not self.allow_hardware_calibration:
            raise PermissionError("hardware calibration control requires --allow-hardware-calibration")
        if self.controller.state == ProviderState.DISCONNECTED:
            self.controller.start()
        if self.controller.state == ProviderState.READ_ONLY:
            self.controller.enable()
        lease=self.controller.acquire_lease(str(payload.get("holder","calibration_gui")),int(payload.get("duration_ms",700)))
        return {"lease_id":lease.lease_id,"fencing_generation":lease.fencing_generation,"expires_in_ms":int((lease.expires_monotonic-time.monotonic())*1000)}

    def renew_lease(self,payload:dict[str,Any]) -> dict[str,Any]:
        started=time.monotonic()
        lease=self.controller.renew_lease(str(payload["lease_id"]),int(payload["fencing_generation"]),int(payload.get("duration_ms",700)))
        elapsed_ms=(time.monotonic()-started)*1000.0
        if elapsed_ms>100.0:
            print(f"[basic-ingress] slow endpoint=lease-renew elapsed_ms={elapsed_ms:.1f}")
        return {"lease_id":lease.lease_id,"fencing_generation":lease.fencing_generation,"expires_in_ms":int((lease.expires_monotonic-time.monotonic())*1000),"ingress_latency_ms":elapsed_ms}

    def command(self,payload:dict[str,Any]) -> dict[str,Any]:
        started=time.monotonic()
        commands={}
        for raw in payload.get("commands",[]):
            index=int(raw["joint_index"]); mode=str(raw["mode"]); values=dict(raw.get("values",{}))
            commands[index]=JointCommand(mode,values)
        timeout_ms=int(payload.get("timeout_ms",250))
        envelope=CommandEnvelope(str(payload.get("command_id",uuid.uuid4())),str(payload["lease_id"]),int(payload["fencing_generation"]),
                                 commands,time.monotonic()+timeout_ms/1000.0)
        self.controller.submit(envelope)
        elapsed_ms=(time.monotonic()-started)*1000.0
        if elapsed_ms>100.0:
            print(f"[basic-ingress] slow endpoint=command elapsed_ms={elapsed_ms:.1f}")
        return {"accepted":True,"command_id":envelope.command_id,"state":self.controller.state.value,"ingress_latency_ms":elapsed_ms}

    def run_experiment(self,payload:dict[str,Any]) -> dict[str,Any]:
        if not self.simulation and not self.allow_hardware_calibration:
            raise PermissionError("hardware calibration experiments require --allow-hardware-calibration")
        if not bool(payload.get("workspace_confirmed",False)):
            raise PermissionError("workspace confirmation is required")
        if not self.experiment_lock.acquire(blocking=False): raise RuntimeError("another experiment is running")
        self.experiment_cancel_event.clear()
        try:
            return self._run_experiment_locked(payload)
        finally:
            self.experiment_cancel_event.clear()
            self.experiment_lock.release()

    def _run_experiment_locked(self,payload:dict[str,Any]) -> dict[str,Any]:
        joint_index=int(payload["joint_index"])
        minimum=float(payload["minimum_rad"])
        maximum=float(payload["maximum_rad"])
        speeds=[float(x) for x in payload.get("speeds_rad_s",[0.16,0.32])]
        if len(speeds)<2:
            speeds=(speeds+[speeds[0]*2.0])[:2]
        speeds=sorted(max(0.05,min(float(value),0.5)) for value in speeds[:2])
        if speeds[1]-speeds[0] < 0.06:
            raise ValueError("the two friction-test speeds must differ by at least 0.06 rad/s")
        save_raw_samples=bool(payload.get("save_raw_samples",False))
        if minimum>=maximum:
            raise ValueError("minimum_rad must be lower than maximum_rad")
        operation=self.configuration.operational_limits[joint_index]
        if minimum<operation[0] or maximum>operation[1]:
            raise ValueError("experiment range is outside the provider operational range")
        if self.controller.state == ProviderState.DISCONNECTED:
            self.controller.start()
        if self.controller.state == ProviderState.READ_ONLY:
            self.controller.enable()

        snapshot=self.controller.snapshot()
        measured=np.asarray(snapshot.get("positions_rad",[]),dtype=float)
        if measured.shape!=(7,):
            raise RuntimeError("seven-joint measured state is required")
        anchor=np.asarray(payload.get("anchor_positions_rad",measured.tolist()),dtype=float)
        if anchor.shape!=(7,) or not np.all(np.isfinite(anchor)):
            raise ValueError("anchor_positions_rad must contain seven finite joint angles")
        for index,(value,limits) in enumerate(zip(anchor,self.configuration.operational_limits)):
            if value<limits[0] or value>limits[1]:
                raise ValueError(f"anchor joint {index+1} is outside the provider operational range")
        non_test=[index for index in range(7) if index!=joint_index]
        if non_test and float(np.max(np.abs(measured[non_test]-anchor[non_test])))>0.12:
            raise RuntimeError("arm moved away from the captured calibration pose; recapture the gravity-float pose")
        if not (minimum<=anchor[joint_index]<=maximum):
            raise ValueError("captured joint angle must be inside its requested calibration range")

        metadata={
            "joint_index":joint_index,
            "minimum_rad":minimum,
            "maximum_rad":maximum,
            "anchor_positions_rad":anchor.tolist(),
            "speeds_rad_s":speeds,
            "control_mode":"POSITION_VELOCITY_LIMITED",
            "save_raw_samples":save_raw_samples,
            "excitation_profile":"FRICTION_TWO_SPEED_POS_VEL_V2",
            "factory_gravity_retained":True,
            "simulation":self.simulation,
            "model_revision":self.configuration.model_revision,
            "calibration_revision":self.configuration.calibration_revision,
        }
        session_id,path=self.recorder.start(metadata,write_metadata=False)
        lease=self.controller.acquire_lease(f"experiment:{session_id}",1500)
        samples:list[dict[str,Any]]=[]
        started=time.monotonic()
        hold_active=False
        try:
            anchor_value=float(anchor[joint_index])

            # Automatic calibration uses POS_VEL for every joint. Six joints
            # hold the captured pose while the selected joint performs the test.
            # No MIT command is used anywhere in this experiment path.
            self.controller.request_position_hold(
                "friction calibration armed", positions=anchor, velocity_limit=min(speeds[0], 0.20)
            )
            self._sweep_to(joint_index,anchor_value,speeds[0],lease,samples,started,session_id,"anchor_entry",anchor,record=False)
            self._sweep_to(joint_index,minimum,speeds[0],lease,samples,started,session_id,"positioning_to_minimum",anchor,record=False)
            self._sweep_to(joint_index,maximum,speeds[0],lease,samples,started,session_id,"friction_slow_positive",anchor,record=True)
            self._sweep_to(joint_index,minimum,speeds[0],lease,samples,started,session_id,"friction_slow_negative",anchor,record=True)
            self._sweep_to(joint_index,maximum,speeds[1],lease,samples,started,session_id,"friction_fast_positive",anchor,record=True)
            self._sweep_to(joint_index,minimum,speeds[1],lease,samples,started,session_id,"friction_fast_negative",anchor,record=True)
            self._sweep_to(joint_index,anchor_value,speeds[0],lease,samples,started,session_id,"anchor_return",anchor,record=False)

            # Keep a full motor-side position hold active before fitting or file
            # work. The user explicitly releases this hold to gravity-float.
            self.controller.request_position_hold(
                "friction calibration motion completed", positions=anchor, velocity_limit=min(speeds[0], 0.20)
            )
            hold_active=True

            fit=fit_two_parameter_friction(samples)
            status="ACCEPTED" if fit.accepted else "MANUAL_REVIEW_REQUIRED"
            result={
                "session_id":session_id,
                "status":status,
                "fit":fit.to_dict(),
                "pair_count":fit.pair_count,
                "raw_sample_count":len(samples),
                "anchor_positions_rad":anchor.tolist(),
                "excitation_profile":"FRICTION_TWO_SPEED_POS_VEL_V2",
                "factory_gravity_retained":True,
                "metadata":metadata,
            }
            if save_raw_samples:
                self.recorder.write_samples(path,samples)
            self.recorder.write_result(path,result)
            return result
        finally:
            if not hold_active:
                try:
                    self.controller.request_position_hold("friction calibration interrupted")
                except Exception:
                    pass
            self.controller.release_lease(lease.lease_id,lease.fencing_generation,fallback_to_float=False)

    def _check_experiment_state(self) -> None:
        if self.experiment_cancel_event.is_set():
            self.controller.request_position_hold("experiment cancelled")
            raise RuntimeError("experiment cancelled into speed-limited hold")
        if self.controller.state in {ProviderState.FAULTED,ProviderState.EMERGENCY_DISABLED}:
            raise RuntimeError("provider faulted during experiment")

    def _record_experiment_sample(self,index:int,target:float,speed:float,phase:str,samples:list[dict[str,Any]],start:float) -> dict[str,Any]:
        snapshot=self.controller.snapshot()
        positions=[float(value) for value in snapshot["positions_rad"]]
        velocities=[float(value) for value in snapshot["velocities_rad_s"]]
        torques=[float(value) for value in snapshot["torques_nm"]]
        sample={
            "time_s":time.monotonic()-start,
            "phase":phase,
            "target_position_rad":float(target),
            "commanded_speed_rad_s":float(speed),
            "position_rad":positions[index],
            "velocity_rad_s":velocities[index],
            "measured_torque_nm":torques[index],
            # Retained for diagnostics only. Factory gravity is not fitted.
            "nominal_gravity_nm":float(self.controller.dynamics.nominal_gravity_component(positions,index)),
        }
        samples.append(sample)
        return sample

    def _send_experiment_target(self,index:int,target:float,speed:float,lease,samples:list[dict[str,Any]],start:float,session_id:str,phase:str,anchor:np.ndarray,record:bool=True) -> dict[str,Any]:
        self._check_experiment_state()
        self.controller.renew_lease(lease.lease_id,lease.fencing_generation,1500)
        hold_speed=min(max(float(speed),0.05),0.20)
        commands={
            joint_index:JointCommand("POSITION_VELOCITY_LIMITED",{
                "position_rad":float(target if joint_index==index else anchor[joint_index]),
                "velocity_limit_rad_s":float(speed if joint_index==index else hold_speed),
            })
            for joint_index in range(7)
        }
        envelope=CommandEnvelope(
            f"{session_id}:{phase}:{time.monotonic_ns()}",
            lease.lease_id,
            lease.fencing_generation,
            commands,
            time.monotonic()+0.3,
        )
        self.controller.submit(envelope)
        # 20 Hz recording and refresh is enough for friction fitting and keeps
        # raw sessions compact while remaining well inside the command deadline.
        time.sleep(0.05)
        if record:
            return self._record_experiment_sample(index,target,speed,phase,samples,start)
        snapshot=self.controller.snapshot()
        return {
            "position_rad":float(snapshot["positions_rad"][index]),
            "velocity_rad_s":float(snapshot["velocities_rad_s"][index]),
        }

    def _sweep_to(self,index:int,target:float,speed:float,lease,samples:list[dict[str,Any]],start:float,session_id:str,phase:str,anchor:np.ndarray,record:bool=True) -> None:
        current=float(self.controller.snapshot().get("positions_rad",[target]*7)[index])
        deadline=time.monotonic()+max(4.0,abs(target-current)/max(speed,0.01)*2.8)
        settled=0
        while time.monotonic()<deadline:
            sample=self._send_experiment_target(index,target,speed,lease,samples,start,session_id,phase,anchor,record=record)
            if abs(float(sample["position_rad"])-target)<0.015 and abs(float(sample["velocity_rad_s"]))<0.05:
                settled+=1
                if settled>=3:
                    return
            else:
                settled=0
        raise TimeoutError(f"joint {index+1} did not reach experiment target")

    def _dwell(self,index:int,target:float,duration_s:float,speed:float,lease,samples:list[dict[str,Any]],start:float,session_id:str,phase:str,anchor:np.ndarray) -> None:
        deadline=time.monotonic()+duration_s
        while time.monotonic()<deadline:
            self._send_experiment_target(index,target,speed,lease,samples,start,session_id,phase,anchor)

    def _multisine_excitation(self,index:int,minimum:float,maximum:float,anchor_value:float,speed:float,lease,samples:list[dict[str,Any]],start:float,session_id:str,anchor:np.ndarray) -> None:
        left=anchor_value-minimum
        right=maximum-anchor_value
        geometric_amplitude=0.80*min(left,right)
        base_frequency_hz=0.12
        harmonics=((0.55,1.0,0.0),(0.30,2.2,np.pi/3.0),(0.15,3.4,np.pi/7.0))
        velocity_factor=sum(weight*multiple for weight,multiple,_ in harmonics)
        speed_amplitude=0.85*speed/(2*np.pi*base_frequency_hz*velocity_factor)
        amplitude=min(0.35,geometric_amplitude,speed_amplitude)
        if amplitude<0.04:
            return
        duration=max(16.0,2.2/base_frequency_hz)
        ramp=min(1.5,duration*0.12)
        started=time.monotonic()
        while True:
            elapsed=time.monotonic()-started
            if elapsed>=duration:
                break
            if elapsed<ramp:
                envelope=0.5-0.5*np.cos(np.pi*elapsed/ramp)
            elif elapsed>duration-ramp:
                envelope=0.5-0.5*np.cos(np.pi*(duration-elapsed)/ramp)
            else:
                envelope=1.0
            signal=0.0
            for weight,multiple,phase in harmonics:
                signal+=weight*np.sin(2*np.pi*base_frequency_hz*multiple*elapsed+phase)
            target=float(np.clip(anchor+envelope*amplitude*signal,minimum,maximum))
            self._send_experiment_target(index,target,speed,torque_ratio,lease,samples,start,session_id,"multisine")
        self._sweep_to(index,anchor,speed,torque_ratio,lease,samples,start,session_id,"multisine_return")

    def apply_fit(self,payload:dict[str,Any]) -> dict[str,Any]:
        index=int(payload["joint_index"])
        fit=dict(payload["fit"])
        joint=self.configuration.joints[index]
        if not bool(fit.get("accepted",False)) or not bool(fit.get("friction_identifiable",False)):
            raise ValueError("only a validated two-parameter friction fit can be applied")

        target=self.configuration.calibration_by_name[joint.name]
        quality=target.setdefault("quality",{})
        coulomb=float(fit.get("coulomb_friction_nm",fit.get("coulomb_friction",0.0)))
        viscous=float(fit.get("viscous_friction_nm_per_rad_s",fit.get("viscous_friction",0.0)))
        target["coulomb_friction_nm"]=coulomb
        target["coulomb_friction_positive_nm"]=coulomb
        target["coulomb_friction_negative_nm"]=coulomb
        target["viscous_friction_nm_per_rad_s"]=viscous
        quality["friction"]="CALIBRATED_TWO_PARAMETER"
        quality["gravity"]="NOMINAL_RETAINED"
        quality["gravity_phase"]="NOT_CALIBRATED"
        quality["effective_inertia"]="NOT_CALIBRATED"
        quality["breakaway"]="NOT_CALIBRATED"
        target["last_fit_metrics"]={key:fit.get(key) for key in (
            "training_rms_residual_nm","validation_rms_residual_nm","validation_max_residual_nm",
            "condition_number","pair_count","slow_pair_count","fast_pair_count",
            "slow_speed_rad_s","fast_speed_rad_s","factory_gravity_retained")}

        qualities=[item.setdefault("quality",{}) for item in self.configuration.calibration["joints"]]
        values=[q.get("friction","UNMEASURED") for q in qualities]
        if all(value.startswith("CALIBRATED") for value in values):
            self.configuration.calibration["quality"]["friction"]="CALIBRATED"
        elif any(value.startswith("CALIBRATED") for value in values):
            self.configuration.calibration["quality"]["friction"]="PARTIAL"
        else:
            self.configuration.calibration["quality"]["friction"]="UNMEASURED"
        self.configuration.calibration["quality"]["gravity"]="NOMINAL_URDF"
        self.configuration.calibration["quality"]["effective_inertia"]="UNMEASURED"

        self.configuration.calibration["calibration_revision"]=f"cal-{time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())}-{uuid.uuid4().hex[:6]}"
        self.configuration.save_calibration(self.calibration_path)

        # Publish the updated intrinsic model immediately; normal publication
        # continues periodically afterward.
        try:
            self.publisher.publish(
                "robot_arm.model",
                "physical_agent.robot_arm_model",
                self.configuration.public_model(),
                self.configuration.model["frames"]["base"],
                self.configuration.calibration_revision,
                5000,
            )
        except Exception as error:
            self.publisher.last_error=str(error)

        return {
            "saved":True,
            "calibration_revision":self.configuration.calibration_revision,
            "joint_quality":quality,
            "overall_quality":self.configuration.calibration["quality"],
            "factory_gravity_retained":True,
        }

    def _handler_type(self):
        service=self
        class Handler(BaseHTTPRequestHandler):
            server_version="RebotArmProvider/0.1.21"
            def log_message(self,format,*args):
                # HTTP access lines are suppressed. Meaningful lifecycle and lease
                # events are emitted explicitly by the controller/service.
                return
            @staticmethod
            def _client_disconnected(error: BaseException) -> bool:
                return isinstance(error,(BrokenPipeError,ConnectionAbortedError,ConnectionResetError)) or (
                    isinstance(error,OSError) and getattr(error,"winerror",None) in {10053,10054}
                )
            def _json(self,status:int,payload:dict[str,Any]):
                data=json.dumps(payload).encode('utf-8')
                try:
                    self.send_response(status)
                    self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(data)))
                    self.send_header('Access-Control-Allow-Origin','*')
                    self.end_headers()
                    self.wfile.write(data)
                except OSError as error:
                    if self._client_disconnected(error):
                        return None
                    raise
                return None
            def _body(self):
                length=int(self.headers.get('Content-Length','0'))
                if length==0:
                    return {}
                try:
                    return json.loads(self.rfile.read(length))
                except OSError as error:
                    if self._client_disconnected(error):
                        return {}
                    raise
            def do_OPTIONS(self):
                try:
                    self.send_response(204); self.send_header('Access-Control-Allow-Origin','*'); self.send_header('Access-Control-Allow-Headers','Content-Type'); self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS'); self.end_headers()
                except OSError as error:
                    if not self._client_disconnected(error):
                        raise
            def do_GET(self):
                try:
                    if self.path=='/health': return self._json(200,service.health())
                    if self.path=='/v1/arm/model': return self._json(200,service.configuration.public_model())
                    if self.path=='/v1/arm/state':
                        state=service.controller.snapshot()
                        if 'positions_rad' in state: state['kinematic_points_m']=service.kinematics.points(state['positions_rad'])
                        return self._json(200,state)
                    return self._json(404,{"error":"not found"})
                except OSError as error:
                    if self._client_disconnected(error): return None
                    return self._json(500,{"error":str(error)})
                except Exception as error: return self._json(500,{"error":str(error)})
            def do_POST(self):
                try:
                    body=self._body()
                    if self.path=='/v1/control/hot':
                        if service.controller.state == ProviderState.DISCONNECTED:
                            service.controller.start()
                        recovery = None
                        if (
                            service.controller.state == ProviderState.FAULTED
                            or service.controller.health == "FAULTED"
                        ):
                            recovery = service.controller.recover_fault_to_gravity_float()
                            if not recovery.get("recovered", False):
                                return self._json(409,{
                                    "status":"fault_recovery_pending",
                                    "state":service.controller.state.value,
                                    "recovery":recovery,
                                })
                        if not service.read_only and service.controller.state == ProviderState.READ_ONLY:
                            service.controller.enable()
                        return self._json(200,{
                            "status":"hot",
                            "state":service.controller.state.value,
                            "fault_recovery":recovery,
                        })
                    if self.path=='/v1/control/warm':
                        if service.controller.state in {ProviderState.DISCONNECTED, ProviderState.READ_ONLY}:
                            return self._json(200,{"status":"already_warm","success":True})
                        success=service.controller.enter_warm(); return self._json(200 if success else 409,{"status":"warm" if success else "gravity_float_retained","success":success})
                    if self.path=='/v1/control/stop':
                        threading.Thread(target=lambda:service.shutdown(True),daemon=True).start(); return self._json(202,{"status":"safe_home_then_stop"})
                    if self.path=='/v1/control/request':
                        return self._json(200,service.handle_manager_request(body))
                    if self.path=='/v1/control/lease': return self._json(200,service.acquire_operational_lease(body))
                    if self.path=='/v1/control/lease/renew': return self._json(200,service.renew_operational_lease(body))
                    if self.path=='/v1/control/lease/release': return self._json(200,service.release_operational_lease(body))
                    if self.path=='/v1/control/command': return self._json(200,service.operational_command(body))
                    if self.path=='/v1/control/payload': return self._json(200,service.operational_payload(body))
                    if self.path=='/v1/calibration/lease': return self._json(200,service.acquire_lease(body))
                    if self.path=='/v1/calibration/lease/renew': return self._json(200,service.renew_lease(body))
                    if self.path=='/v1/calibration/command': return self._json(200,service.command(body))
                    if self.path=='/v1/calibration/gravity-float':
                        if service.controller.state == ProviderState.DISCONNECTED: service.controller.start()
                        if service.controller.state == ProviderState.READ_ONLY: service.controller.enable()
                        service.controller.request_gravity_float(str(body.get('reason','external gravity-float request'))); return self._json(200,{"status":"gravity_float","reason":str(body.get('reason','external gravity-float request'))})
                    if self.path=='/v1/calibration/safe-home':
                        if service.controller.state == ProviderState.DISCONNECTED: service.controller.start()
                        max_velocity_rad_s=body.get("max_velocity_rad_s")
                        success=service.controller.safe_home(
                            max_velocity_rad_s=(
                                None
                                if max_velocity_rad_s is None
                                else float(max_velocity_rad_s)
                            )
                        )
                        return self._json(200,{
                            "success":success,
                            "details":service.controller.snapshot().get("last_safe_home_result"),
                        })
                    if self.path=='/v1/calibration/experiment': return self._json(200,service.run_experiment(body))
                    if self.path=='/v1/calibration/experiment/cancel': service.experiment_cancel_event.set(); service.controller.request_position_hold('experiment cancelled'); return self._json(200,{'status':'cancelling_into_position_hold'})
                    if self.path=='/v1/calibration/apply-fit': return self._json(200,service.apply_fit(body))
                    return self._json(404,{"error":"not found"})
                except LeasePermissionError as error:
                    status = 409 if error.error_code in {
                        "ACTIVE_LEASE_CONFLICT",
                        "STALE_LEASE",
                        "OPERATIONAL_CONTROL_BLOCKED",
                    } else 403
                    return self._json(status,{
                        "error":error.reason,
                        "reason":error.reason,
                        "error_code":error.error_code,
                        "lease_status":error.lease_status,
                        "last_lease_event":service.controller.snapshot().get("lease_diagnostics",{}).get("last_event"),
                    })
                except PermissionError as error: return self._json(403,{"error":str(error),"reason":str(error),"error_code":"PERMISSION_DENIED"})
                except (ValueError,KeyError,TimeoutError) as error: return self._json(400,{"error":str(error)})
                except OSError as error:
                    if self._client_disconnected(error): return None
                    return self._json(500,{"error":str(error)})
                except Exception as error: return self._json(500,{"error":str(error)})
        return Handler
