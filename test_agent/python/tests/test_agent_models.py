from __future__ import annotations

import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agents import Agent, FunctionTool, OpenAIChatCompletionsModel, ToolSearchTool
from agents.exceptions import UserError
from agents.models.chatcmpl_converter import Converter
from agents.run_context import RunContextWrapper

from physical_agent_test.agent_driver import _select_routed_tools
from physical_agent_test.agent_models import (
    GEMINI_OPENAI_BASE_URL,
    agent_model_api_key_name,
    is_gemini_agent_model,
    is_gpt_agent_model,
    narrow_local_tool_search,
    require_agent_model_credential,
    resolve_agent_model,
    supported_agent_reasoning_efforts,
    tools_for_agent_model,
    uses_native_gpt_tool_search,
)


class AgentModelTests(unittest.TestCase):
    def test_gemini_model_uses_google_openai_compatibility_client(self) -> None:
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "unit-test-gemini-key"},
        ):
            model = resolve_agent_model("gemini-3.7-flash")

        self.assertIsInstance(model, OpenAIChatCompletionsModel)
        self.assertEqual(model.model, "gemini-3.7-flash")
        self.assertEqual(str(model._client.base_url), GEMINI_OPENAI_BASE_URL)

    def test_openai_model_keeps_native_agents_sdk_resolution(self) -> None:
        self.assertEqual(resolve_agent_model("gpt-5.6-terra"), "gpt-5.6-terra")
        self.assertTrue(is_gpt_agent_model("gpt-5.6-terra"))
        self.assertTrue(is_gpt_agent_model("GPT-5.6-SOL"))
        self.assertFalse(is_gpt_agent_model("gemini-3.7-flash"))

    def test_provider_specific_credentials_are_required(self) -> None:
        self.assertTrue(is_gemini_agent_model("gemini-3.7-flash"))
        self.assertEqual(
            agent_model_api_key_name("gemini-3.7-flash"),
            "GEMINI_API_KEY",
        )
        self.assertEqual(
            agent_model_api_key_name("gpt-5.6-terra"),
            "OPENAI_API_KEY",
        )
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                require_agent_model_credential("gemini-3.7-flash")

    def test_gemini_reasoning_efforts_match_published_levels(self) -> None:
        self.assertEqual(
            supported_agent_reasoning_efforts("gemini-3.7-flash"),
            ("low", "medium", "high"),
        )
        self.assertEqual(
            supported_agent_reasoning_efforts("gpt-5.6-terra"),
            ("low", "medium", "high", "xhigh", "max"),
        )

    def test_gemini_rebuilds_two_tier_discovery_for_streaming(self) -> None:
        invocations: list[str] = []

        async def invoke(_context, _arguments: str) -> str:
            invocations.append("called")
            return "{}"

        deferred = FunctionTool(
            name="deferred_skill",
            description="Deferred test Skill",
            params_json_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            defer_loading=True,
        )
        tool_search = ToolSearchTool()

        gemini_tools = tools_for_agent_model(
            "gemini-3.7-flash",
            [deferred, tool_search],
        )
        self.assertFalse(
            uses_native_gpt_tool_search("gemini-3.7-flash")
        )
        self.assertEqual(
            [tool.name for tool in gemini_tools],
            ["deferred_skill", "tool_search"],
        )
        self.assertFalse(gemini_tools[0].defer_loading)
        self.assertTrue(deferred.defer_loading)
        state = SimpleNamespace(loaded_tool_names=set())
        context = RunContextWrapper(state)
        agent = Agent(name="two-tier-test", tools=gemini_tools)
        initially_enabled = asyncio.run(agent.get_all_tools(context))
        self.assertEqual(
            [tool.name for tool in initially_enabled],
            ["tool_search"],
        )
        search_result = asyncio.run(
            gemini_tools[1].on_invoke_tool(
                SimpleNamespace(
                    context=state,
                    tool_call_id="search-call-1",
                ),
                '{"paths":["deferred_skill"]}',
            )
        )
        decoded = json.loads(search_result)
        self.assertEqual(decoded["status"], "completed")
        self.assertEqual(decoded["type"], "tool_search_output")
        self.assertEqual(decoded["execution"], "client")
        self.assertEqual(decoded["call_id"], "search-call-1")
        self.assertEqual(
            decoded["tools"][0],
            {
                "type": "function",
                "name": "deferred_skill",
                "description": "Deferred test Skill",
                "defer_loading": True,
                "parameters": deferred.params_json_schema,
                "strict": True,
            },
        )
        self.assertEqual(invocations, [])
        after_search = asyncio.run(agent.get_all_tools(context))
        self.assertEqual(
            [tool.name for tool in after_search],
            ["deferred_skill", "tool_search"],
        )
        fresh_context = RunContextWrapper(
            SimpleNamespace(loaded_tool_names=set())
        )
        fresh_enabled = asyncio.run(agent.get_all_tools(fresh_context))
        self.assertEqual(
            [tool.name for tool in fresh_enabled],
            ["tool_search"],
        )
        self.assertEqual(
            Converter.tool_to_openai(gemini_tools[0])["function"]["name"],
            "deferred_skill",
        )
        self.assertEqual(
            Converter.tool_to_openai(gemini_tools[1])["function"]["name"],
            "tool_search",
        )

        with self.assertRaisesRegex(UserError, "defer_loading=True"):
            Converter.tool_to_openai(deferred)

        openai_tools = tools_for_agent_model(
            "gpt-5.6-terra",
            [deferred, tool_search],
        )
        self.assertTrue(
            uses_native_gpt_tool_search("gpt-5.6-terra")
        )
        self.assertEqual(openai_tools, [deferred, tool_search])

        generalized_tools = tools_for_agent_model(
            "vendor-chat-model",
            [deferred, tool_search],
        )
        self.assertFalse(
            uses_native_gpt_tool_search("vendor-chat-model")
        )
        self.assertEqual(
            [tool.name for tool in generalized_tools],
            ["deferred_skill", "tool_search"],
        )
        self.assertFalse(generalized_tools[0].defer_loading)

    def test_gemini_tool_search_rejects_unknown_paths(self) -> None:
        async def invoke(_context, _arguments: str) -> str:
            return "{}"

        deferred = FunctionTool(
            name="deferred_skill",
            description="Deferred test Skill",
            params_json_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            defer_loading=True,
        )
        tools = tools_for_agent_model(
            "gemini-3.7-flash",
            [deferred, ToolSearchTool()],
        )

        state = SimpleNamespace(loaded_tool_names=set())
        with self.assertLogs(
            "physical_agent_test.agent_models",
            level="WARNING",
        ):
            result = asyncio.run(
                tools[-1].on_invoke_tool(
                    SimpleNamespace(
                        context=state,
                        tool_call_id="search-call-unknown",
                    ),
                    '{"paths":["not_offered"]}',
                )
            )
        decoded = json.loads(result)

        self.assertEqual(decoded["type"], "tool_search_error")
        self.assertEqual(decoded["status"], "failed")
        self.assertEqual(
            decoded["error"]["code"],
            "UNKNOWN_OR_INELIGIBLE_SKILL",
        )
        self.assertTrue(decoded["error"]["retryable"])
        self.assertEqual(decoded["error"]["allowed_paths"], ["deferred_skill"])
        self.assertEqual(state.loaded_tool_names, set())

    def test_gemini_tool_search_recovers_identical_repeated_json(self) -> None:
        async def invoke(_context, _arguments: str) -> str:
            return "{}"

        deferred = FunctionTool(
            name="deferred_skill",
            description="Deferred test Skill",
            params_json_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            defer_loading=True,
        )
        tools = tools_for_agent_model(
            "gemini-3.7-flash",
            [deferred, ToolSearchTool()],
        )
        state = SimpleNamespace(loaded_tool_names=set())
        arguments = '{"paths":["deferred_skill"]}'

        with self.assertLogs(
            "physical_agent_test.agent_models",
            level="WARNING",
        ) as logs:
            result = asyncio.run(
                tools[-1].on_invoke_tool(
                    SimpleNamespace(
                        context=state,
                        tool_call_id="search-call-duplicate",
                    ),
                    arguments + arguments,
                )
            )

        self.assertEqual(json.loads(result)["status"], "completed")
        self.assertEqual(state.loaded_tool_names, {"deferred_skill"})
        self.assertIn("duplicate_count=2", " ".join(logs.output))
        self.assertNotIn(arguments, " ".join(logs.output))

    def test_gemini_tool_search_returns_typed_invalid_json_failure(self) -> None:
        async def invoke(_context, _arguments: str) -> str:
            return "{}"

        deferred = FunctionTool(
            name="deferred_skill",
            description="Deferred test Skill",
            params_json_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            defer_loading=True,
        )
        tools = tools_for_agent_model(
            "gemini-3.7-flash",
            [deferred, ToolSearchTool()],
        )
        state = SimpleNamespace(loaded_tool_names=set())
        arguments = (
            '{"paths":["deferred_skill"]}'
            '{"paths":["different_skill"]}'
        )

        with self.assertLogs(
            "physical_agent_test.agent_models",
            level="WARNING",
        ) as logs:
            result = asyncio.run(
                tools[-1].on_invoke_tool(
                    SimpleNamespace(
                        context=state,
                        tool_call_id="search-call-invalid-json",
                    ),
                    arguments,
                )
            )
        decoded = json.loads(result)

        self.assertEqual(decoded["status"], "failed")
        self.assertEqual(decoded["error"]["code"], "INVALID_JSON")
        self.assertGreater(decoded["diagnostics"]["error_position"], 0)
        self.assertEqual(state.loaded_tool_names, set())
        self.assertNotIn(arguments, " ".join(logs.output))

    def test_gemini_tool_search_can_be_narrowed_to_a_routed_surface(self) -> None:
        async def invoke(_context, _arguments: str) -> str:
            return "{}"

        def deferred(name: str) -> FunctionTool:
            return FunctionTool(
                name=name,
                description=f"Description for {name}",
                params_json_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                on_invoke_tool=invoke,
                defer_loading=True,
            )

        first = deferred("first_skill")
        second = deferred("second_skill")
        tools = tools_for_agent_model(
            "gemini-3.7-flash",
            [first, second, ToolSearchTool()],
        )
        narrowed = narrow_local_tool_search(
            tools[-1],
            {"first_skill", "tool_search"},
        )

        self.assertIsNotNone(narrowed)
        assert narrowed is not None
        self.assertIn("first_skill", narrowed.description)
        self.assertNotIn("second_skill", narrowed.description)
        result = asyncio.run(
            narrowed.on_invoke_tool(
                SimpleNamespace(
                    context=SimpleNamespace(loaded_tool_names=set()),
                    tool_call_id="routed-search",
                ),
                '{"paths":["first_skill"]}',
            )
        )
        self.assertEqual(json.loads(result)["tools"][0]["name"], "first_skill")
        with self.assertLogs(
            "physical_agent_test.agent_models",
            level="WARNING",
        ):
            failure = asyncio.run(
                narrowed.on_invoke_tool(
                    SimpleNamespace(
                        context=SimpleNamespace(loaded_tool_names=set()),
                        tool_call_id="routed-search-invalid",
                    ),
                    '{"paths":["second_skill"]}',
                )
            )
        self.assertEqual(json.loads(failure)["status"], "failed")

    def test_gemini_two_tier_discovery_requires_one_search_source(self) -> None:
        async def invoke(_context, _arguments: str) -> str:
            return "{}"

        deferred = FunctionTool(
            name="deferred_skill",
            description="Deferred test Skill",
            params_json_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            on_invoke_tool=invoke,
            defer_loading=True,
        )

        with self.assertRaisesRegex(ValueError, "exactly one"):
            tools_for_agent_model("gemini-3.7-flash", [deferred])

    def test_gemini_keeps_immediate_tools_visible_before_discovery(self) -> None:
        async def invoke(_context, _arguments: str) -> str:
            return "{}"

        schema = {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        graph = FunctionTool(
            name="run_limited_graph",
            description="Run one bounded Limited Graph",
            params_json_schema=schema,
            on_invoke_tool=invoke,
            defer_loading=False,
        )
        child = FunctionTool(
            name="graph_child",
            description="Run one graph child Skill",
            params_json_schema=schema,
            on_invoke_tool=invoke,
            defer_loading=True,
        )
        tools = tools_for_agent_model(
            "gemini-3.7-flash",
            [graph, child, ToolSearchTool()],
        )
        context = RunContextWrapper(
            SimpleNamespace(loaded_tool_names=set())
        )
        enabled = asyncio.run(
            Agent(name="graph-first-test", tools=tools).get_all_tools(context)
        )

        self.assertEqual(
            [tool.name for tool in enabled],
            ["run_limited_graph", "tool_search"],
        )
        single_skill_route = _select_routed_tools(
            tools,
            {"graph_child"},
        )
        self.assertEqual(
            [tool.name for tool in single_skill_route],
            ["graph_child", "tool_search"],
        )


if __name__ == "__main__":
    unittest.main()
