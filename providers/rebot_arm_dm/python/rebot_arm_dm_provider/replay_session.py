"""Replay a recorded calibration session through the current estimator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .calibration import robust_fit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refit a recorded reBot Arm calibration session")
    parser.add_argument("session", help="Session directory or samples.jsonl path")
    parser.add_argument("--without-inertia", action="store_true")
    parser.add_argument("--write-result", action="store_true")
    args = parser.parse_args(argv)

    source = Path(args.session)
    samples_path = source / "samples.jsonl" if source.is_dir() else source
    if not samples_path.is_file():
        raise FileNotFoundError(samples_path)
    samples = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = robust_fit(samples, include_inertia=not args.without_inertia).to_dict()
    print(json.dumps(result, indent=2))
    if args.write_result:
        target = samples_path.parent / "replayed_result.json"
        target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
