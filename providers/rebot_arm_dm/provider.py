"""Entry point for the reBot Arm DM Basic Controller."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PYTHON=ROOT/'python'
if str(PYTHON) not in sys.path: sys.path.insert(0,str(PYTHON))

from rebot_arm_dm_provider.controller import ArmController
from rebot_arm_dm_provider.dynamics import RebotDynamics
from rebot_arm_dm_provider.hardware import MotorBridgeBackend, SimulationBackend
from rebot_arm_dm_provider.kinematics import RebotKinematics
from rebot_arm_dm_provider.models import ArmConfiguration
from rebot_arm_dm_provider.service import ArmProviderService


def parse_args():
    parser=argparse.ArgumentParser(description="reBot Arm DM Basic Controller")
    parser.add_argument('--config',default=str(ROOT/'config'/'arm_model.json'))
    parser.add_argument('--calibration',default=str(ROOT/'config'/'arm_calibration.json'))
    parser.add_argument('--port',default='COM3'); parser.add_argument('--baudrate',type=int,default=921600)
    parser.add_argument('--simulate',action='store_true'); parser.add_argument('--allow-hardware-calibration',action='store_true'); parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--listen-host',default='127.0.0.1'); parser.add_argument('--listen-port',type=int,default=8791)
    parser.add_argument('--manager-url',default='http://127.0.0.1:7001'); parser.add_argument('--fabric-url',default='http://127.0.0.1:7002')
    return parser.parse_args()


def main() -> int:
    args=parse_args(); configuration=ArmConfiguration.load(args.config,args.calibration)
    kinematics=RebotKinematics(configuration.model); dynamics=RebotDynamics(configuration,kinematics)
    backend=SimulationBackend(configuration,dynamics.calibrated_gravity_torque) if args.simulate else MotorBridgeBackend(configuration,args.port,args.baudrate)
    controller=ArmController(configuration,backend,dynamics)
    service=ArmProviderService(configuration,controller,kinematics,args.calibration,args.listen_host,args.listen_port,
                               args.manager_url,args.fabric_url,args.allow_hardware_calibration,args.simulate,args.read_only)
    shutdown_started = False
    shutdown_lock = threading.Lock()

    def run_graceful_shutdown(signum: int) -> None:
        nonlocal shutdown_started
        success = False
        try:
            success = service.shutdown(True)
        except Exception as error:
            print(f"Graceful shutdown failed: {error}", file=sys.stderr, flush=True)
        if not success:
            print(
                "Safe-home did not complete. Powered gravity-float is retained and "
                "the provider remains running; a later stop request may retry.",
                file=sys.stderr,
                flush=True,
            )
            with shutdown_lock:
                shutdown_started = False

    def graceful_signal_handler(signum, frame):
        nonlocal shutdown_started
        with shutdown_lock:
            if shutdown_started:
                return
            shutdown_started = True
        print(
            f"Signal {signum}: safe-home, powered settle, disable, and exit requested.",
            flush=True,
        )
        threading.Thread(
            target=run_graceful_shutdown, args=(signum,), daemon=True
        ).start()

    signal.signal(signal.SIGINT, graceful_signal_handler)
    signal.signal(signal.SIGTERM, graceful_signal_handler)
    try:
        service.start(); print(f"reBot Arm DM Basic Controller listening at {service.control_url} (simulation={args.simulate})",flush=True); service.wait(); return 0
    except Exception as error:
        print(f"Provider failed: {error}",file=sys.stderr,flush=True)
        try: service.shutdown(False)
        except Exception: pass
        return 1

if __name__=='__main__': raise SystemExit(main())
