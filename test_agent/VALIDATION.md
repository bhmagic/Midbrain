# Validation

The current Limited Graph/context checkpoint is accepted as near stable for
the retained linear physical workflow described in
[Limited Graph Status and Qualification](../docs/14_LIMITED_GRAPH_STATUS_AND_QUALIFICATION.md).
The coverage below does not promote unexercised live branch, switch, retry,
model-route, simultaneous multi-visual, or material-cut sensing behavior to
qualified status.

Run the stopped software suite from the repository root with the Test Agent's
own environment:

```powershell
.\test_agent\.venv\Scripts\python.exe -m pytest -q test_agent\python\tests
```

The suite is expected to validate:

- multi-provider Agent model resolution, including Gemini's documented
  OpenAI-compatible base URL, provider-specific credential selection,
  low/medium/high reasoning contract, and preservation of native GPT model
  resolution;
- model-aware streaming tool surfaces, including native hosted discovery for
  every `gpt-*` model and client-executed discovery for every non-`gpt-*`
  model, exact `paths` and client `tool_search_output` shapes, run-local dynamic
  loading of the original FunctionTools, immediate Limited Graph visibility,
  exclusion of the Responses-only object from compatibility transports,
  canonical search events recovered from real function-output envelopes,
  exact Chat Completions conversion, and preservation of the original GPT
  tool list;
- manifest-only Skill discovery and allowlisting;
- mandatory discovery-v3 input/output schema and two-tier metadata validation
  across every installed Skill, complete field-name publication, compact
  result-pointer publication, normalized complete-result validation,
  sanitization, bounded detail retention, and direct-result projection;
- source-backed coverage for all 23 installed output contracts, including
  representative result-construction tokens, required and forbidden published
  fields, the deliberately empty nested FoundationPose direct contract, and
  Limited Graph manifest/canonical-result alignment;
- adapter binding after selection;
- Provider lifecycle readiness and structured continuation;
- combined explicit scene-policy publication and regulated runtime-catalog
  observation, followed by a separate exact Provider lifecycle call with no
  authority or responsibility transfer;
- concise Limited Graph authoring-schema compilation into canonical version 1,
  including linear defaults, bindings, edge overrides, read-only retry,
  switches, model routes, custom/default terminals, canonical compatibility,
  explicit JSON-bearing field names, one model-visible pre-execution authoring
  correction, second-rejection termination, and unchanged canonical preflight
  and execution;
- equivalent concise initial-binding spellings through `$name#/pointer` and
  `$initial#/name/pointer`, including preservation of a genuinely declared
  initial value named `initial`;
- Limited Graph child-declared Provider handover through the existing
  lifecycle FunctionTool, including unchanged child arguments, fresh call
  identities, lifecycle authorization, bounded repeat handling, and ordered
  trace evidence;
- Limited Graph binding and explicit incomplete-result failure paths,
  compact source/condition/target pointer preflight, leaked noncompact-field
  rejection, physical-cycle rejection, validation-before-sanitization with
  retained credential exclusion, trusted child-owned pre-submission failure
  routing, no physical retry, and physical unknown-outcome stops for every
  unclassified or possibly submitted exception;
- compact Limited Graph `last_failure` publication after failure-edge routing,
  including exact child identity, failure kind, reason, and known physical-
  submission state while full trace data remains detail-only;
- regulated complete Provider/capability catalog projection, sanitized exact
  Provider-detail reads, opaque full/selected Skill-result detail reads,
  session scoping and retention pruning, and exclusion of both detail tools
  from Limited Graph children;
- compound existing-scene work-object motion and mixed-frame slicing discovery,
  including Fabric derivation, absolute-world motion, direction translation,
  and slicing in one eligible graph child catalog;
- graph-contained SAM2/FoundationPose/VLM evidence projection, including
  sanitized child arrays and bounded dictionary-representation tool outputs;
- immediate graph-child visual publication before graph completion, isolated
  run-local relays, and duplicate suppression when the final graph result is
  translated;
- ordered multi-evidence chat projection, legacy single-evidence hydration,
  and independent visual cards on both Agent pages;
- exact slice-point to typed-offset to absolute-world-motion schema preflight,
  with Fabric frame identity preserved and no model coordinate arithmetic;
- standalone and compound trailing Safe Home routing, including a typed Basic
  Provider activation continuation when the controller is disconnected;
- canonical streaming-run creation, replay, terminal status, and decisions;
- task-scoped cancellation, pending-action cleanup, terminal `CANCELLED`
  replay, and preservation of background Providers;
- SDK-neutral Agent event projection;
- visual evidence, normalized annotations, channel applicability, and persisted
  FoundationPose/VLM/RGB-D artifact visibility across Agent and aligner web
  process boundaries;
- bounded user-image upload and attachment separation;
- Manager-boot chat projection and journal retention;
- camera capture and visual-inference retry boundaries;
- spatial-frame and convention enforcement;
- nonphysical preview and exact execution-plan integrity; and
- failure paths that must not submit or duplicate physical action.

Live nonphysical validation should then confirm current Manager/Fabric
identity, Provider activation, readable RGB-D evidence, visual-backend
behavior, browser reconnection, and journal presentation without enabling arm
execution.

Physical Agent validation is a separate, explicitly authorized test. It must
bind the exact objective, Provider identities, evidence, scene, preview,
authority, limits, controller outcome, post-action observation, safe-home, and
shutdown result. A Provider-handover case must additionally retain the graph
run and digest, root/child/lifecycle call identities, ordered handover trace,
physical-action count, calibration/frame provenance, and terminal result. A
model selecting a tool does not by itself validate those boundaries.

Test counts and dated checkpoint results belong in generated test output and
[the changelog](CHANGELOG.md), not in this evergreen validation contract.
