# Agent Image Attachment Contract

Status: v0.1 development contract.

## Purpose

This contract lets an operator add image context to an Agent turn without
making the browser or the Midbrain HTTP API depend on an agent SDK's native
message classes. It also keeps user-supplied imagery distinct from robot
camera evidence.

## Upload and reference boundary

The browser first uploads one still image to `POST /api/agent-attachments`.
The development implementation accepts JPEG, PNG, or WebP image bytes encoded
as base64 JSON. It verifies the encoded size, decodes the image, checks the
actual format and dimensions, rejects animation, and returns a Midbrain-owned
`attachment_id` plus bounded metadata. The current limits are 8 MiB and 40
million decoded pixels.

A run from either Agent view then supplies at most one ID in
`attachment_ids`. Raw image bytes are not repeated in the run-creation
request, SSE events, approval cards, or run-status payloads. A missing or
expired reference is rejected before an Agent run starts.

The current attachment store is bounded process memory: 64 images for up to
30 minutes. It is suitable for the development UI, not durable field history.

## Agent adapter boundary

Midbrain resolves the attachment ID and gives the selected Agent runtime a
text-plus-image user message. The initial OpenAI adapter projects that message
onto Responses-format `input_text` and `input_image` content. Another agent
runtime may project the same Midbrain attachment onto its own native format
without changing the browser contract.

Text-only turns retain the prior string input shape. Legacy synchronous and
backend-owned streaming endpoints accept the same attachment references.

## Evidence and authority boundary

A user attachment is contextual input, not a Fabric observation. It has no
Provider identity, capture timestamp, calibration, depth registration, spatial
frame, evidence freshness, or robot-state authority. It must never satisfy a
Skill requirement for a current camera frame or authorize physical action.

The current upload is interpreted by the selected intellectual Agent model.
The Robotics-ER visual model selector continues to control finite Skills that
capture live robot-camera imagery. Those routes remain separate unless a
future Skill explicitly accepts a user attachment and declares its provenance.

## Privacy and retention

The image is sent to the selected model as part of the Agent input. The local
Agents SDK session may persist the resulting multimodal input in
`agent_sessions.sqlite3` so later turns retain conversational context. This is
separate from the 30-minute upload-store retention and must be considered when
implementing session deletion, durable chat history, trace policy, or field
deployment data governance.

The current loopback-only API has no credential gate. Attachment endpoints
therefore share the long-term authentication, authorization, origin, rate-limit,
and audit work already required for all Agent endpoints before remote exposure.
