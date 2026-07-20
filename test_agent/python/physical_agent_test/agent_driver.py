from __future__ import annotations

import json
import os

from agents import Agent, Runner, function_tool

from .gemini_pointing_skill import PointingIdentificationSkill


class PrototypeAgentDriver:
    def __init__(self, skill: PointingIdentificationSkill, model: str):
        self.skill = skill

        @function_tool
        async def identify_pointed_object(question: str) -> str:
            """Capture the latest RGB image and identify the object a person points at."""
            return await self.skill.run(question)

        self.agent = Agent(
            name="Physical Agent Prototype Driver",
            model=model,
            instructions=(
                "You are the temporary top-level driver for a physical-agent platform test. "
                "For any request about the current camera scene, a visible person, pointing, "
                "or identifying an object, call identify_pointed_object. The tool uses one RGB "
                "frame and returns JSON. Explain the result plainly and mention uncertainty. "
                "Do not invent depth information, robot actions, or additional sensor evidence. "
                "For unrelated requests, state that this prototype currently tests only the "
                "pointed-object RGB skill."
            ),
            tools=[identify_pointed_object],
        )

    async def run(self, prompt: str) -> str:
        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY is empty in config/api_keys.env")
        result = await Runner.run(self.agent, prompt, max_turns=5)
        output = result.final_output
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, default=str)
