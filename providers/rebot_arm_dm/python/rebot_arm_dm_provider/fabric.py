"""Small JSON HTTP clients for Manager registration and Fabric observations."""
from __future__ import annotations

from typing import Any
from urllib import request
import json
import threading
import time


class JsonHttpClient:
    def __init__(self, timeout: float = 3.0):
        self.timeout = timeout

    def post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(payload).encode('utf-8')
        req = request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read()
        return {} if not raw else json.loads(raw)

    def get(self, url: str) -> dict[str, Any]:
        req = request.Request(url, method='GET')
        with request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read()
        return {} if not raw else json.loads(raw)


class PlatformPublisher:
    def __init__(
        self,
        provider_id: str,
        instance_id: str,
        boot_id: str,
        manager_url: str | None,
        fabric_url: str | None,
    ):
        self.provider_id = provider_id
        self.instance_id = instance_id
        self.boot_id = boot_id
        self.manager_url = manager_url.rstrip('/') if manager_url else None
        self.fabric_url = fabric_url.rstrip('/') if fabric_url else None
        self.http = JsonHttpClient()
        self.sequence: dict[str, int] = {}
        self.lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.last_successful_output_us: dict[str, int] = {}
        self.manager_error: str | None = None
        self.fabric_error: str | None = None
        self.last_error: str | None = None

    def _refresh_last_error(self) -> None:
        self.last_error = self.manager_error or self.fabric_error

    def status_payload(self, state: dict[str, Any], control_url: str) -> dict[str, Any]:
        disconnected = (
            state.get('state') == 'DISCONNECTED'
            or state.get('provider_state') == 'DISCONNECTED'
        )
        ready = (
            not disconnected
            and state.get('state') not in {'FAULTED', 'EMERGENCY_DISABLED'}
            and state.get('provider_state') not in {'FAULTED', 'EMERGENCY_DISABLED'}
        )
        details = dict(state)
        details.setdefault(
            'capability_readiness',
            {
                'robot.motion.arm.basic': bool(ready and state.get('midbrain_motion_allowed', False)),
                'robot_arm.joint_state': bool(ready),
                'robot_arm.gravity_float': bool(ready),
                'robot_arm.control.impedance': bool(ready and state.get('midbrain_motion_allowed', False)),
            },
        )
        return {
            'provider_id': self.provider_id,
            'provider_type': self.provider_id,
            'instance_id': self.instance_id,
            'boot_id': self.boot_id,
            'residency': 'WARM' if disconnected else 'HOT',
            'health': state.get('health', 'UNKNOWN'),
            'ready': ready,
            'pid': __import__('os').getpid(),
            'control_url': control_url,
            'details': details,
        }

    def register(self, state: dict[str, Any], control_url: str) -> None:
        if not self.manager_url:
            return
        try:
            response = self.http.post(
                f'{self.manager_url}/v1/providers/register',
                self.status_payload(state, control_url),
            )
            if response.get('accepted') is False:
                raise RuntimeError('Midbrain Manager rejected provider registration')
        except Exception as exc:
            self.manager_error = str(exc)
            self._refresh_last_error()
            raise
        self.manager_error = None
        self._refresh_last_error()

    def heartbeat(self, state: dict[str, Any], control_url: str) -> None:
        if not self.manager_url:
            return
        try:
            response = self.http.post(
                f'{self.manager_url}/v1/providers/heartbeat',
                self.status_payload(state, control_url),
            )
            if response.get('accepted') is False:
                raise RuntimeError('Midbrain Manager rejected provider heartbeat')
        except Exception as exc:
            self.manager_error = str(exc)
            self._refresh_last_error()
            raise
        self.manager_error = None
        self._refresh_last_error()


    def motion_inhibit(self) -> dict[str, Any]:
        if not self.manager_url:
            return {'inhibited': False, 'owners': [], 'enforcement': 'NO_MANAGER_URL'}
        try:
            payload = self.http.get(f'{self.manager_url}/v1/motion/inhibit')
            if not isinstance(payload, dict):
                raise ValueError('Midbrain motion-inhibit response must be an object')
        except Exception as exc:
            self.manager_error = str(exc)
            self._refresh_last_error()
            raise
        self.manager_error = None
        self._refresh_last_error()
        return payload

    def _next_sequence(self, stream: str) -> int:
        with self.lock:
            sequence = self.sequence.get(stream, 0) + 1
            self.sequence[stream] = sequence
            return sequence

    def observation(
        self,
        stream: str,
        schema: str,
        data: dict[str, Any],
        frame_id: str | None,
        calibration_revision: str,
        freshness_ms: int = 200,
        observed_at_us: int | None = None,
    ) -> dict[str, Any]:
        return {
            'schema': schema,
            'schema_version': 1,
            'stream': stream,
            'provider_id': self.provider_id,
            'provider_instance_id': self.instance_id,
            'boot_id': self.boot_id,
            'sequence': self._next_sequence(stream),
            'observed_at_us': int(observed_at_us or time.time_ns() // 1000),
            'freshness_ms': freshness_ms,
            'frame_id': frame_id,
            'coordinate_frame': 'RIGHT_HANDED_Z_UP',
            'calibration_revision': calibration_revision,
            'clock_domain': 'system_wall_clock',
            'related_skill_id': None,
            'valid': True,
            'data': data,
        }

    def _post_fabric(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.fabric_url:
            return {}
        try:
            response = self.http.post(f'{self.fabric_url}{path}', payload)
        except Exception as exc:
            self.fabric_error = str(exc)
            self._refresh_last_error()
            raise
        self.fabric_error = None
        self._refresh_last_error()
        return response

    def _record_successful_output(self, key: str, observed_at_us: int) -> None:
        with self.output_lock:
            self.last_successful_output_us[key] = int(observed_at_us)

    def output_status(self, key: str) -> dict[str, int | float | None]:
        with self.output_lock:
            observed_at_us = self.last_successful_output_us.get(key)
        return {
            'observed_at_us': observed_at_us,
            'age_ms': (
                None
                if observed_at_us is None
                else max(0.0, (time.time_ns() // 1000 - observed_at_us) / 1000.0)
            ),
        }

    def publish(
        self,
        stream: str,
        schema: str,
        data: dict[str, Any],
        frame_id: str | None,
        calibration_revision: str,
        freshness_ms: int = 200,
        observed_at_us: int | None = None,
    ) -> None:
        if not self.fabric_url:
            return
        observation = self.observation(
            stream,
            schema,
            data,
            frame_id,
            calibration_revision,
            freshness_ms,
            observed_at_us,
        )
        response = self._post_fabric(
            '/v1/observations',
            observation,
        )
        if response.get('accepted') is False:
            raise RuntimeError(f'Fabric did not accept {stream}')
        self._record_successful_output(stream, int(observation['observed_at_us']))

    def publish_batch(
        self,
        observations: list[dict[str, Any]],
        success_key: str | None = None,
    ) -> None:
        if not self.fabric_url or not observations:
            return
        response = self._post_fabric('/v1/observations/batch', {'observations': observations})
        accepted = response.get('accepted')
        if accepted is False:
            raise RuntimeError('Fabric rejected the observation batch')
        if type(accepted) is int and accepted != len(observations):
            raise RuntimeError(
                f'Fabric accepted {accepted} of {len(observations)} observations'
            )
        if success_key:
            self._record_successful_output(
                success_key,
                max(int(item['observed_at_us']) for item in observations),
            )

    def errors(self) -> dict[str, str | None]:
        return {'manager': self.manager_error, 'fabric': self.fabric_error}
