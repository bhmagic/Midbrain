# Current Test-Agent Progress

## Working

- OpenAI Agents SDK driver with one function tool.
- Finite Skill that captures one exact RGB BufferRef and calls Gemini Robotics-ER.
- Minimum browser prompt UI.
- RGB and diagnostic depth previews.
- Status queries for Manager, Fabric, and configured providers.
- API keys isolated in the workspace `config/api_keys.env` file.

## Deliberately temporary

- No persistent mission state.
- No agent handoffs, recovery graph, approvals, or long-running execution state.
- No generic Skill registry or Skill package loader.
- No 3D pointing geometry; Gemini currently interprets one RGB frame.
- No operator authentication or permission model.
- No robot motion tools.

This scaffold exists only to exercise the Manager, Fabric, provider transport, and a complete agent-to-Skill request path.
