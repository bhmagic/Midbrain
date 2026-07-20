# Current Test Flow

1. The browser sends a prompt to the local FastAPI UI.
2. The OpenAI Agents SDK driver decides whether to call `identify_pointed_object`.
3. The Skill asks the Fabric for the latest `camera.rgb.frame_ref`.
4. The Skill opens the named shared-memory mapping provided by the Orbbec package.
5. It validates the slot generation and reads that exact RGB payload.
6. Only the RGB image is sent to Gemini Robotics-ER.
7. The answer returns through the Skill and agent to the browser.

Depth preview is a local diagnostic path and is not sent to Gemini in this Skill.
