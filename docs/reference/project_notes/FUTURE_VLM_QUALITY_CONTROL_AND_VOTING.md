# Future VLM Quality Control and Voting

Status: far future; intentionally not enabled in Phase 2

The current `VisionLanguageRouter` supports an ordered list of VLM backends.
It records failed attempts and returns the backend and model that produced the
accepted result. This permits a lower-cost model to fall back to a larger model
when the first call fails.

Phase 2 does not let another VLM approve, reject, score, or vote on a result.
`quality_control_mode` is locked to `OFF_FUTURE`. This avoids presenting an
untested model consensus as a safety mechanism.

Before the arm leaves the safeguarded environment, future work may evaluate:

- a second-model review of spatial labels and segmentation;
- independent multi-model voting;
- deterministic geometry checks before any model review;
- disagreement thresholds that force operator authorization;
- cost, latency, and call-count budgets;
- recording every candidate answer, reviewer output, and final selection;
- failure injection for correlated hallucinations and unavailable services.

VLM QC must remain advisory evidence. It cannot replace deterministic motion
limits, workcell authorization, provider lease fencing, controller collision
checks, or the physical execution gate.
