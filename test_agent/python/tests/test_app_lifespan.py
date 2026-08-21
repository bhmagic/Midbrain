from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from physical_agent_test import app as app_module


class AppLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_continues_after_one_close_failure(self) -> None:
        stream_registry = SimpleNamespace(shutdown=AsyncMock())
        world_point_cloud = SimpleNamespace(stop=AsyncMock())
        locate_arm_base_skill = SimpleNamespace(
            close=Mock(side_effect=RuntimeError("locate close failed"))
        )
        basic = SimpleNamespace(close=AsyncMock())
        integrated = SimpleNamespace(close=AsyncMock())
        fabric = SimpleNamespace(close=AsyncMock())
        manager = SimpleNamespace(close=AsyncMock())
        journal = SimpleNamespace(close=AsyncMock())

        with (
            patch.object(
                app_module,
                "agent_run_stream_registry",
                stream_registry,
            ),
            patch.object(app_module, "world_point_cloud", world_point_cloud),
            patch.object(
                app_module,
                "locate_arm_base_skill",
                locate_arm_base_skill,
            ),
            patch.object(app_module, "basic", basic),
            patch.object(app_module, "integrated", integrated),
            patch.object(app_module, "fabric", fabric),
            patch.object(app_module, "manager", manager),
            patch.object(app_module, "agent_run_journal", journal),
        ):
            with self.assertRaisesRegex(RuntimeError, "locate close failed"):
                await app_module._close_lifespan_resources()

        stream_registry.shutdown.assert_awaited_once_with()
        world_point_cloud.stop.assert_awaited_once_with()
        locate_arm_base_skill.close.assert_called_once_with()
        basic.close.assert_awaited_once_with()
        integrated.close.assert_awaited_once_with()
        fabric.close.assert_awaited_once_with()
        manager.close.assert_awaited_once_with()
        journal.close.assert_awaited_once_with()

    async def test_lifespan_closes_locate_arm_base_on_exception(self) -> None:
        manager = SimpleNamespace(
            health=AsyncMock(return_value={"boot_id": "test-boot"}),
            close=AsyncMock(),
        )
        journal = SimpleNamespace(
            start=AsyncMock(),
            close=AsyncMock(),
        )
        policy_publisher = SimpleNamespace(
            restore_policy=AsyncMock(return_value=None)
        )
        world_point_cloud = SimpleNamespace(
            start=AsyncMock(),
            stop=AsyncMock(),
        )
        stream_registry = SimpleNamespace(shutdown=AsyncMock())
        basic = SimpleNamespace(close=AsyncMock())
        integrated = SimpleNamespace(close=AsyncMock())
        fabric = SimpleNamespace(close=AsyncMock())
        locate_arm_base_skill = SimpleNamespace(close=Mock())
        settings = SimpleNamespace(auto_initialize_space_cognition=False)

        with (
            patch.object(app_module, "manager", manager),
            patch.object(app_module, "agent_run_journal", journal),
            patch.object(
                app_module,
                "scene_segmentation_policy_publisher",
                policy_publisher,
            ),
            patch.object(app_module, "world_point_cloud", world_point_cloud),
            patch.object(
                app_module,
                "agent_run_stream_registry",
                stream_registry,
            ),
            patch.object(app_module, "basic", basic),
            patch.object(app_module, "integrated", integrated),
            patch.object(app_module, "fabric", fabric),
            patch.object(
                app_module,
                "locate_arm_base_skill",
                locate_arm_base_skill,
            ),
            patch.object(app_module, "settings", settings),
            patch.object(app_module, "auto_initialization_task", None),
        ):
            with self.assertRaisesRegex(RuntimeError, "test shutdown"):
                async with app_module.lifespan(None):
                    raise RuntimeError("test shutdown")

        locate_arm_base_skill.close.assert_called_once_with()
        stream_registry.shutdown.assert_awaited_once_with()
        world_point_cloud.stop.assert_awaited_once_with()
        basic.close.assert_awaited_once_with()
        integrated.close.assert_awaited_once_with()
        fabric.close.assert_awaited_once_with()
        manager.close.assert_awaited_once_with()
        journal.close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
