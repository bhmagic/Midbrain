from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .skill_catalog import AgentSkillDescriptor
from .skill_execution import SkillExecutionAdapter


_FACTORY_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ExternalSkillHostServices:
    """Platform services available to optional Skill-owned host plug-ins."""

    manager: Any
    fabric: Any
    spatial: Any
    vlm_router: Any
    visual_evidence_store: Any
    integrated_motion: Any = None
    contact_provider_url: str = "http://127.0.0.1:8794"
    skill_invocation_broker: Any = None


def load_external_skill_host_adapters(
    descriptors: list[AgentSkillDescriptor],
    *,
    eligible_tool_names: set[str],
    services: ExternalSkillHostServices,
) -> dict[str, SkillExecutionAdapter]:
    """Load declared host glue from eligible Skill directories only."""

    adapters: dict[str, SkillExecutionAdapter] = {}
    for descriptor in descriptors:
        if (
            not descriptor.discoverable
            or descriptor.tool_name not in eligible_tool_names
        ):
            continue
        entrypoint = descriptor.host_adapter_entrypoint
        factory_name = descriptor.host_adapter_factory
        if entrypoint is None and factory_name is None:
            continue
        if entrypoint is None or factory_name is None:
            raise RuntimeError(
                f"{descriptor.tool_name} has incomplete host-adapter metadata"
            )
        if not _FACTORY_NAME.fullmatch(factory_name):
            raise RuntimeError(
                f"{descriptor.tool_name} has an invalid host-adapter factory"
            )

        manifest_path = Path(descriptor.manifest_path).resolve()
        skill_root = manifest_path.parent
        entrypoint_path = (skill_root / entrypoint).resolve()
        if skill_root not in entrypoint_path.parents:
            raise RuntimeError(
                f"{descriptor.tool_name} host adapter escaped its Skill directory"
            )
        if not entrypoint_path.is_file():
            raise RuntimeError(
                f"{descriptor.tool_name} host adapter is unavailable: "
                f"{entrypoint_path}"
            )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        module = _load_module(entrypoint_path, descriptor.execution_adapter_id)
        factory = getattr(module, factory_name, None)
        if not callable(factory):
            raise RuntimeError(
                f"{descriptor.tool_name} host-adapter factory is unavailable"
            )
        adapter = factory(
            skill_root=skill_root,
            manifest=manifest,
            services=services,
        )
        if inspect.isawaitable(adapter):
            raise RuntimeError(
                f"{descriptor.tool_name} host-adapter factory must be synchronous"
            )
        if not callable(getattr(adapter, "invoke", None)):
            raise RuntimeError(
                f"{descriptor.tool_name} host adapter has no invoke method"
            )
        if descriptor.execution_adapter_id in adapters:
            raise RuntimeError(
                "duplicate external Skill host adapter: "
                f"{descriptor.execution_adapter_id}"
            )
        adapters[descriptor.execution_adapter_id] = adapter
    return adapters


def _load_module(entrypoint: Path, adapter_id: str) -> ModuleType:
    digest = hashlib.sha256(
        f"{adapter_id}\0{entrypoint}".encode("utf-8")
    ).hexdigest()[:16]
    module_name = f"midbrain_external_skill_host_{digest}"
    specification = importlib.util.spec_from_file_location(
        module_name,
        entrypoint,
    )
    if specification is None or specification.loader is None:
        raise RuntimeError(f"cannot load external Skill host adapter: {entrypoint}")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module
