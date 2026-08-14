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

from rebot_arm_contact.basic_client import BasicControllerClient
from rebot_arm_contact.controller import ContactController
from rebot_arm_contact.platform import PlatformPublisher
from rebot_arm_contact.service import ContactService


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Independent reBot Contact Work Provider")
    parser.add_argument("--config", default=str(ROOT / "config/controller.json"))
    parser.add_argument("--basic-url", default=None)
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
        raise FileNotFoundError(
            f"Contact Provider config is missing: {path}; run scripts/setup.ps1"
        )
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Contact Provider configuration must be an object")
    return value


def main(argv=None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    if args.basic_url:
        config["basic"]["url"] = args.basic_url
    control_url = f"http://{config['listen_host']}:{config['listen_port']}"
    platform = PlatformPublisher(
        str(config["provider_id"]),
        str(config["provider_type"]),
        args.manager_url,
        args.fabric_url,
        control_url,
    )
    basic = BasicControllerClient(str(config["basic"]["url"]))
    controller = ContactController(
        config,
        basic,
        provider_instance_id=platform.instance_id,
        provider_boot_id=platform.boot_id,
    )
    service = ContactService(controller, config, platform)
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        if stopping:
            return
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
                print(f"[contact-startup] Basic is not ready: {exc}; retrying...")
                time.sleep(0.5)
        print(f"Contact Work Provider: {control_url}")
        print("The Provider is independent from Integrated and commands Basic directly.")
        print("Every active session remains in POSITION_EFFORT_LIMITED until replacement or relax.")
        while not service.shutdown_event.wait(0.25):
            pass
    except Exception as exc:
        print(f"Contact Work Provider failed: {exc}", file=sys.stderr)
        try:
            service.shutdown()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
