from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_matrix(value: str | None) -> list[float]:
    if not value:
        return [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]

    path = Path(value).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8-sig"))

    if isinstance(payload, dict):
        payload = payload.get("mesh_from_semantic")

    if not isinstance(payload, list) or len(payload) != 16:
        raise ValueError(
            "--mesh-from-semantic-json must contain a 16-value array "
            "or an object with mesh_from_semantic."
        )

    return [float(item) for item in payload]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate or update a FoundationPose model registry entry."
    )
    parser.add_argument("--registry", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--role", default="generic_object")
    parser.add_argument("--description", default="")
    parser.add_argument("--mesh-path", required=True)
    parser.add_argument("--semantic-frame", required=True)
    parser.add_argument("--default-child-frame", default="")
    parser.add_argument("--scale-to-m", type=float, required=True)
    parser.add_argument("--revision", default="cad-prepared")
    parser.add_argument("--mesh-from-semantic-json")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace an existing entry with the same model_id.",
    )
    args = parser.parse_args()

    registry_path = Path(args.registry).expanduser().resolve()
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    if registry_path.exists():
        document = json.loads(
            registry_path.read_text(encoding="utf-8-sig")
        )
    else:
        document = {
            "revision": "custom-foundationpose-registry-v1",
            "models": [],
        }

    models = document.setdefault("models", [])

    if not isinstance(models, list):
        raise ValueError("registry models must be an array")

    existing = [
        entry
        for entry in models
        if isinstance(entry, dict)
        and str(entry.get("model_id")) == args.model_id
    ]

    if existing and not args.replace:
        raise ValueError(
            f"model_id already exists: {args.model_id}; use --replace"
        )

    mesh_path = Path(args.mesh_path).expanduser().resolve()

    try:
        stored_mesh_path = mesh_path.relative_to(
            registry_path.parent
        ).as_posix()
    except ValueError:
        stored_mesh_path = str(mesh_path)

    entry = {
        "model_id": args.model_id,
        "role": args.role,
        "description": args.description,
        "mesh_path": stored_mesh_path,
        "semantic_frame": args.semantic_frame,
        "default_child_frame": (
            args.default_child_frame or None
        ),
        "mesh_from_semantic": parse_matrix(
            args.mesh_from_semantic_json
        ),
        "scale_to_m": args.scale_to_m,
        "symmetry": {
            "type": "NONE",
        },
        "enabled": True,
        "revision": args.revision,
    }

    document["models"] = [
        item
        for item in models
        if not (
            isinstance(item, dict)
            and str(item.get("model_id")) == args.model_id
        )
    ] + [entry]

    registry_path.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps(entry, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
