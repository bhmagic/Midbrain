# Current Test-Agent Progress

## Working

- OpenAI Agents SDK driver with one allowlisted function tool in the initial
  narrow evaluation.
- Manifest-only Skill discovery that does not import or start implementations.
- Separate adapter binding after discovery, with detailed input schema loaded
  only for offered Skills.
- Finite Skill that captures one exact RGB BufferRef and calls Gemini Robotics-ER.
- Advisory Manager capability binding with explicit provider fallback.
- Generic RGB-D route preference with direct Orbbec fallback.
- Per-decision authorization records and a browser popup; approval never
  executes an action.
- A nonphysical front/top end-effector observation proposal and Integrated
  controller preview request.
- Minimum browser prompt UI.
- RGB and diagnostic depth previews.
- Status queries for Manager, Fabric, and configured providers.
- API keys isolated in the workspace `config/api_keys.env` file.

## Deliberately temporary

- No persistent mission state.
- No agent handoffs, recovery graph, or long-running execution state.
- The manifest catalog is a discovery registry, not a general package
  installer or unrestricted dynamic code loader.
- No 3D pointing geometry; Gemini currently interprets one RGB frame.
- No operator authentication or permission model.
- No agent-accessible robot motion execution tool. The observation pose path
  remains preview-only and outside the initial allowlist.

This scaffold exists only to exercise the Manager, Fabric, provider transport, and a complete agent-to-Skill request path.
