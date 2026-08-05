from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "python"))

from rebot_arm_integrated.basic_client import BasicControllerClient, LeaseLostError
from rebot_arm_integrated.config_repair import ensure_controller_config
from rebot_arm_integrated.controller import IntegratedController
from rebot_arm_integrated.service import IntegratedService


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="reBot Arm MIT Cartesian bring-up controller")
    parser.add_argument("--config", default=str(ROOT / "config/controller.json"))
    parser.add_argument("--manager-url", default=os.getenv("PHYSICAL_AGENT_MANAGER_URL", "http://127.0.0.1:7001"))
    parser.add_argument("--fabric-url", default=os.getenv("PHYSICAL_AGENT_FABRIC_URL", "http://127.0.0.1:7002"))
    parser.add_argument("--basic-url", default=None)
    parser.add_argument("--scene", default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main():
    args = parse_args()
    repair = ensure_controller_config(ROOT, Path(args.config))
    config = repair.config
    if repair.repaired:
        print(f"[staged-config] replaced obsolete Integrated configuration from {repair.source}")
    if args.basic_url:
        config["basic_controller_url"] = args.basic_url

    basic = BasicControllerClient(
        config["basic_controller_url"],
        command_timeout=max(0.05, float(config["command_timeout_ms"]) / 1000.0),
    )
    controller = IntegratedController(config, basic)
    service = IntegratedService(controller, config, args.manager_url, args.fabric_url)
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        source = "operator request" if signum is None else f"signal {signum}"
        print(f"Arm Integrated staged controller: shutdown requested by {source}; floating and releasing its fenced Basic lease...")
        service.shutdown()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        startup_deadline = time.monotonic() + 15.0
        while True:
            try:
                service.start()
                break
            except LeaseLostError:
                raise
            except Exception as exc:
                retryable = isinstance(exc, (TimeoutError, urlerror.URLError, ConnectionError))
                if not retryable or time.monotonic() >= startup_deadline:
                    raise
                basic.clear_lease()
                print(f"[staged-startup] Basic Controller not ready: {exc}; retrying...")
                time.sleep(0.5)
        print(f"Arm Integrated motion prototype UI: {service.control_url}")
        print("PRESS_MIT ONE_SHOT and HOLD_LB are marked usable.")
        print("TRANSIT_SPEED/POS_VEL ONE_SHOT accepts IK-valid free-space requests up to 1.2 m; joint, scene, and motor limits remain authoritative.")
        print("TRANSIT_SPEED HOLD_LB and CONTACT_WORK/POS_TOR ONE_SHOT remain GUI-only experimental/unstable modes and are not advertised through capability discovery.")
        print("Physical arm motion uses Engage plus LB; Basic MIT support runs at 50 Hz and latched motor endpoints refresh at 10 Hz.")
        print("ONE_SHOT: LB rising edge commits once.")
        print("HOLD_LB: replan toward the staged target while held; LB release returns to gravity-float.")
        print("Selectable 3-DoF position IK and 6-DoF pose IK are available.")
        print("Fabric can update the staged Cartesian target, but it does not bypass GUI Engage + Xbox LB authority.")
        print("Tool acting-point offset and fenced Basic payload gravity compensation are enabled.")
        print("Semantic sphere collision previews and float torque-baseline capture are enabled without granting motion authority.")
        print("Gripper MIT/POS_TOR endpoint tests are available on RB/RT and latch after release.")
        print("CONTACT_WORK requires 6-DoF, a separately captured float baseline, and a JOINT_6, WRENCH_6, or ISOTROPIC_2 budget.")
        while not service.shutdown_event.wait(0.25):
            pass
    except Exception as exc:
        print(f"Integrated staged controller failed: {exc}", file=sys.stderr)
        try:
            service.shutdown()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
