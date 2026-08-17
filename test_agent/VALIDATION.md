# Validation

Run the stopped software suite from the repository root with the Test Agent's
own environment:

```powershell
.\test_agent\.venv\Scripts\python.exe -m pytest -q test_agent\python\tests
```

The suite is expected to validate:

- manifest-only Skill discovery and allowlisting;
- mandatory discovery-v2 input/output schema validation across every installed
  Skill, exact result-pointer publication, and normalized direct-result
  validation;
- source-backed coverage for all 23 installed output contracts, including
  representative result-construction tokens, required and forbidden published
  fields, the deliberately empty nested FoundationPose direct contract, and
  Limited Graph manifest/canonical-result alignment;
- adapter binding after selection;
- Provider lifecycle readiness and structured continuation;
- Limited Graph child-declared Provider handover through the existing
  lifecycle FunctionTool, including unchanged child arguments, fresh call
  identities, lifecycle authorization, bounded repeat handling, and ordered
  trace evidence;
- Limited Graph binding and explicit incomplete-result failure paths,
  declared source/condition/target pointer preflight, physical-cycle
  rejection, validation-before-redaction with retained credential exclusion,
  no physical retry, and physical unknown-outcome stops;
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
