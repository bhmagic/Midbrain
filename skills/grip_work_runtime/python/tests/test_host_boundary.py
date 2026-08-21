from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import sys
import uuid


def test_agent_loaded_grip_adapters_do_not_import_private_packages() -> None:
    workspace = Path(__file__).resolve().parents[4]
    entrypoints = (
        workspace / "skills/grip/python/grip_skill/host_adapter.py",
        workspace / "skills/grip-object/python/grip_object_skill/host_adapter.py",
        workspace
        / "skills/move-carried-object/python/move_carried_object_skill/host_adapter.py",
        workspace / "skills/let-go/python/let_go_skill/host_adapter.py",
        workspace / "skills/lay-flat/python/lay_flat_skill/host_adapter.py",
    )
    private_prefixes = (
        "grip_work_runtime",
        "grip_skill",
        "grip_object_skill",
        "move_carried_object_skill",
        "let_go_skill",
        "lay_flat_skill",
    )
    original_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith(private_prefixes):
            raise AssertionError(
                f"Agent-loaded host adapter imported private package {name}"
            )
        return original_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded_import
    try:
        for entrypoint in entrypoints:
            module_name = f"grip_host_boundary_{uuid.uuid4().hex}"
            specification = importlib.util.spec_from_file_location(
                module_name,
                entrypoint,
            )
            assert specification is not None
            assert specification.loader is not None
            module = importlib.util.module_from_spec(specification)
            sys.modules[module_name] = module
            try:
                specification.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
    finally:
        builtins.__import__ = original_import
