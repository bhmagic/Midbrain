# Validation

Run the stopped software suite from the repository root with the Test Agent's
own environment:

```powershell
.\test_agent\.venv\Scripts\python.exe -m pytest -q test_agent\python\tests
```

The suite is expected to validate:

- manifest-only Skill discovery and allowlisting;
- adapter binding after selection;
- Provider lifecycle readiness and structured continuation;
- Limited Graph child-declared Provider handover through the existing
  lifecycle FunctionTool, including unchanged child arguments, fresh call
  identities, lifecycle authorization, bounded repeat handling, and ordered
  trace evidence;
- Limited Graph binding and explicit incomplete-result failure paths,
  physical-cycle rejection, no physical retry, and physical unknown-outcome
  stops;
- canonical streaming-run creation, replay, terminal status, and decisions;
- task-scoped cancellation, pending-action cleanup, terminal `CANCELLED`
  replay, and preservation of background Providers;
- SDK-neutral Agent event projection;
- visual evidence, normalized annotations, and channel applicability;
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
