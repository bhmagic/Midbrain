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
    parser = argparse.ArgumentParser(description="Arm Integrated free-space controller")
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
        print(f"[free-space-config] replaced obsolete Integrated configuration from {repair.source}")
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
        print(f"Arm Integrated free-space controller: shutdown requested by {source}; floating and releasing its fenced Basic lease...")
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
                print(f"[free-space-startup] Basic Controller not ready: {exc}; retrying...")
                time.sleep(0.5)
        print(f"Arm Integrated read-only developer UI: {service.control_url}")
        print("Physical free-space motion uses only the signed path-plan/path-commit boundary.")
        print("Position-only, orientation-only, arbitrary 3D, and combined 6-DoF goals are supported.")
        print("The direct path or its closest-safe prefix is evaluated; general obstacle rerouting is not implemented.")
        print("Contact, gripping, manual target staging, runtime settings, gamepad teleoperation, and Fabric command input are not exposed.")
        while not service.shutdown_event.wait(0.25):
            pass
    except Exception as exc:
        print(f"Integrated free-space controller failed: {exc}", file=sys.stderr)
        try:
            service.shutdown()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
