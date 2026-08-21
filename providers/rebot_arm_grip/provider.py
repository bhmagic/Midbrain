from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "python"))

from rebot_arm_grip.basic_client import BasicControllerClient
from rebot_arm_grip.controller import GripController
from rebot_arm_grip.http_client import JsonHttpClient
from rebot_arm_grip.platform import PlatformPublisher
from rebot_arm_grip.service import GripService


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Independent reBot Grip Provider")
    parser.add_argument("--config", default=str(ROOT / "config/controller.json"))
    parser.add_argument("--basic-url", default=None)
    parser.add_argument("--contact-url", default=None)
    parser.add_argument(
        "--manager-url",
        default=os.getenv("PHYSICAL_AGENT_MANAGER_URL", "http://127.0.0.1:7001"),
    )
    parser.add_argument(
        "--fabric-url",
        default=os.getenv("PHYSICAL_AGENT_FABRIC_URL", "http://127.0.0.1:7002"),
    )
    return parser.parse_args(argv)


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Grip Provider config is missing: {path}; run scripts/setup.ps1")
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Grip Provider configuration must be an object")
    return value


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    if args.basic_url:
        config["basic"]["url"] = args.basic_url
    if args.contact_url:
        config["contact"]["url"] = args.contact_url
    profile_path = (ROOT / str(config["effector_control_profile_path"])).resolve()
    config["effector_control_profile"] = load_config(profile_path)
    control_url = f"http://{config['listen_host']}:{config['listen_port']}"
    platform = PlatformPublisher(
        str(config["provider_id"]),
        str(config["provider_type"]),
        args.manager_url,
        args.fabric_url,
        control_url,
    )
    controller = GripController(
        config,
        BasicControllerClient(str(config["basic"]["url"])),
        JsonHttpClient(1.5),
        provider_instance_id=platform.instance_id,
        provider_boot_id=platform.boot_id,
    )
    service = GripService(controller, config, platform)
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        if not stopping:
            stopping = True
            service.shutdown()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        deadline = time.monotonic() + 15.0
        while True:
            try:
                service.start()
                break
            except (TimeoutError, urlerror.URLError, ConnectionError) as exc:
                if time.monotonic() >= deadline:
                    raise
                print(f"[grip-startup] Basic is not ready: {exc}; retrying...")
                time.sleep(0.5)
        print(f"Grip Provider: {control_url}")
        print("Gripper persistence and carrying state are isolated from arm motion providers.")
        while not service.shutdown_event.wait(0.25):
            pass
    except Exception as exc:
        print(f"Grip Provider failed: {exc}", file=sys.stderr)
        try:
            service.shutdown()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
