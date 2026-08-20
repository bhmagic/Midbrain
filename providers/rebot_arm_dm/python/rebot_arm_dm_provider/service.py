"""Provider HTTP service and platform publication."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import json
import threading
import time
import uuid

from .assembly import RobotAssemblyConfiguration
from .controller import ArmController, CommandEnvelope, JointCommand, ProviderState, LeasePermissionError
from .fabric import PlatformPublisher
from .kinematics import RebotKinematics
from .models import ArmConfiguration


class ArmProviderService:
    def __init__(self, configuration: ArmConfiguration, controller: ArmController, kinematics: RebotKinematics,
                 listen_host: str, listen_port: int,
                 manager_url: str|None = None, fabric_url: str|None = None,
                 allow_hardware_calibration: bool = False, simulation: bool = False,
                 read_only: bool = False,
                 assembly: RobotAssemblyConfiguration|None = None):
        self.configuration=configuration; self.controller=controller; self.kinematics=kinematics
        self.listen_host=listen_host; self.listen_port=listen_port
        self.control_url=f"http://{listen_host}:{listen_port}"; self.instance_id=str(uuid.uuid4()); self.boot_id=str(uuid.uuid4())
        self.publisher=PlatformPublisher("robot_arm.rebot_dm",self.instance_id,self.boot_id,manager_url,fabric_url)
        self.allow_hardware_calibration=allow_hardware_calibration; self.simulation=simulation
        self.read_only=read_only
        self.assembly=assembly
        if self.assembly is not None:
            self.controller.configure_resource_groups(
                str(self.assembly.selection["arm_resource_id"]),
                self.assembly.resource_groups(),
                self.assembly.inactive_joint_names,
            )
        self.shutdown_event=threading.Event(); self.httpd:ThreadingHTTPServer|None=None
        self.platform_threads:list[threading.Thread]=[]; self.model_published=False
        self.manager_registered=False
        self.motion_inhibited=False
        self.motion_inhibit_owners=[]
        self.platform_safety_block_active=False
        self.manager_request_results: dict[str, dict[str, Any]] = {}
        self.manager_request_lock = threading.Lock()

    def start(self) -> None:
        self.controller.start()
        # Physical startup is gravity-supported by default. Attended development
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
        assembly_output = self.publisher.output_status("robot_arm.assembly_state")
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
                    "robot_arm.assembly_state": self.assembly is not None,
                    "robot_arm.control.joint_group.v1": bool(
                        motion_allowed and self.assembly is not None
                    ),
                    "robot_arm.control.concurrent_disjoint_groups.v1": bool(
                        motion_allowed and self.assembly is not None
                    ),
                },
                "publication_outputs": {
                    "robot_arm.joint_state": joint_output,
                    "robot_arm.transforms.local": transform_output,
                    "robot_arm.assembly_state": assembly_output,
                },
                "held_control_authority_leases": [],
                "provider_local_control_lease": lease,
                "provider_local_control_leases": state.get(
                    "resource_group_leases", []
                ),
                "resource_groups": (
                    self.assembly.resource_groups()
                    if self.assembly is not None
                    else []
                ),
                "manager_authority_lease_supported": False,
                "provider_group_control_supported": bool(
                    self.assembly is not None
                ),
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
            self._group_resource_id(payload),
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
                    if self.assembly is not None:
                        self.publisher.publish(
                            "robot_arm.assembly_state",
                            "midbrain.robot_assembly_state",
                            self.assembly.public_state(),
                            self.configuration.model["frames"]["base"],
                            self.assembly.fingerprint,
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
            group_resource_id = self._group_resource_id(request_payload)
            if group_resource_id is not None:
                self.controller.request_group_float(
                    group_resource_id,
                    str(request_payload.get("reason", "manager request")),
                )
                result = {
                    "status": "group_gravity_float",
                    "resource_id": group_resource_id,
                }
            else:
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
            details = self.controller.safe_home_result()
            result = {
                "status": "safe_home",
                "success": success,
                "termination_allowed": bool(
                    details.get("termination_allowed", success)
                ),
                "safe_state_confirmed": bool(
                    details.get("physical_outcome_known", success)
                ),
                "details": details,
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

    def _group_resource_id(self, payload: dict[str, Any]) -> str | None:
        """Return a requested child resource, or None for root authority."""

        resource_id = str(payload.get("resource_id") or "").strip()
        # Root leases carry their canonical resource ID in service responses,
        # so field presence alone cannot distinguish root from group authority.
        if not resource_id or resource_id == self.controller.resource_root:
            return None
        return resource_id


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
        group_resource_id = self._group_resource_id(payload)
        with self.controller.ingress_lock:
            control_was_active = bool(
                self.controller.lease is not None
                or self.controller.group_leases
            )
        if group_resource_id is not None:
            lease = self.controller.acquire_group_lease(
                group_resource_id,
                str(payload.get("holder", "runtime_controller")),
                int(payload.get("duration_ms", self.configuration.model["control"]["lease_timeout_ms"])),
            )
        else:
            lease = self.controller.acquire_lease(
                str(payload.get("holder", "runtime_controller")),
                int(payload.get("duration_ms", self.configuration.model["control"]["lease_timeout_ms"])),
            )
        if not control_was_active:
            self.controller.request_gravity_float("operational lease acquisition")
        return {
            "lease_id": lease.lease_id,
            "fencing_generation": lease.fencing_generation,
            "resource_id": lease.resource_id,
            "expires_in_ms": int((lease.expires_monotonic-time.monotonic())*1000),
        }

    def release_operational_lease(self, payload: dict[str, Any]) -> dict[str, Any]:
        group_resource_id = self._group_resource_id(payload)
        if group_resource_id is not None:
            siblings_remain = self.controller.release_group_lease(
                group_resource_id,
                str(payload["lease_id"]),
                int(payload["fencing_generation"]),
            )
            if not siblings_remain:
                status = "released_gravity_float"
            else:
                status = "released_group_sibling_control_retained"
        else:
            self.controller.release_lease(
                str(payload["lease_id"]),
                int(payload["fencing_generation"]),
                fallback_to_float=True,
            )
            status = "released_gravity_float"
        return {
            "status": status,
            "resource_id": group_resource_id or self.controller.resource_root,
        }

    def acquire_lease(self,payload:dict[str,Any]) -> dict[str,Any]:
        if self.read_only:
            raise PermissionError("control leases are blocked in explicit read-only mode")
        if not self.simulation and not self.allow_hardware_calibration:
            raise PermissionError(
                "attended hardware development control requires the compatibility flag "
                "--allow-hardware-calibration"
            )
        if self.controller.state == ProviderState.DISCONNECTED:
            self.controller.start()
        if self.controller.state == ProviderState.READ_ONLY:
            self.controller.enable()
        lease=self.controller.acquire_lease(str(payload.get("holder","calibration_gui")),int(payload.get("duration_ms",700)))
        return {"lease_id":lease.lease_id,"fencing_generation":lease.fencing_generation,"expires_in_ms":int((lease.expires_monotonic-time.monotonic())*1000)}

    def renew_lease(self,payload:dict[str,Any]) -> dict[str,Any]:
        started=time.monotonic()
        group_resource_id=self._group_resource_id(payload)
        lease=(
            self.controller.renew_group_lease(
                group_resource_id,
                str(payload["lease_id"]),
                int(payload["fencing_generation"]),
                int(payload.get("duration_ms",700)),
            )
            if group_resource_id is not None
            else self.controller.renew_lease(str(payload["lease_id"]),int(payload["fencing_generation"]),int(payload.get("duration_ms",700)))
        )
        elapsed_ms=(time.monotonic()-started)*1000.0
        if elapsed_ms>100.0:
            print(f"[basic-ingress] slow endpoint=lease-renew elapsed_ms={elapsed_ms:.1f}")
        return {"lease_id":lease.lease_id,"fencing_generation":lease.fencing_generation,"resource_id":lease.resource_id,"expires_in_ms":int((lease.expires_monotonic-time.monotonic())*1000),"ingress_latency_ms":elapsed_ms}

    def command(self,payload:dict[str,Any]) -> dict[str,Any]:
        started=time.monotonic()
        commands={}
        for raw in payload.get("commands",[]):
            index=int(raw["joint_index"]); mode=str(raw["mode"]); values=dict(raw.get("values",{}))
            commands[index]=JointCommand(mode,values)
        timeout_ms=int(payload.get("timeout_ms",250))
        group_resource_id=self._group_resource_id(payload)
        envelope=CommandEnvelope(str(payload.get("command_id",uuid.uuid4())),str(payload["lease_id"]),int(payload["fencing_generation"]),
                                 commands,time.monotonic()+timeout_ms/1000.0,resource_id=group_resource_id)
        if group_resource_id is not None:
            self.controller.submit_group(envelope)
        else:
            self.controller.submit(envelope)
        elapsed_ms=(time.monotonic()-started)*1000.0
        if elapsed_ms>100.0:
            print(f"[basic-ingress] slow endpoint=command elapsed_ms={elapsed_ms:.1f}")
        return {"accepted":True,"command_id":envelope.command_id,"resource_id":group_resource_id or self.controller.resource_root,"state":self.controller.state.value,"ingress_latency_ms":elapsed_ms}

    def _handler_type(self):
        service=self
        class Handler(BaseHTTPRequestHandler):
            server_version="RebotArmProvider/0.1.24"
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
                    if self.path=='/v1/arm/assembly':
                        if service.assembly is None:
                            return self._json(404,{"error":"no robot assembly selection is active"})
                        return self._json(200,service.assembly.public_state())
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
                        details=service.controller.safe_home_result()
                        return self._json(200,{
                            "success":success,
                            "termination_allowed":bool(
                                details.get("termination_allowed",success)
                            ),
                            "safe_state_confirmed":bool(
                                details.get("physical_outcome_known",success)
                            ),
                            "details":details,
                        })
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
