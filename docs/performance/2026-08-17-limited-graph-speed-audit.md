# Limited Graph speed audit

Date: 2026-08-17

Current implementation acceptance and open live qualification are maintained
in [Limited Graph Status and Qualification](../14_LIMITED_GRAPH_STATUS_AND_QUALIFICATION.md).
This dated document remains the measurement record.

For the current long-to-short inventory of Agent, Provider, Skill-compute,
controller-settling, and graph waiting with signed physical trajectory time
removed, see the
[current non-motion wait inventory](2026-08-17-current-non-motion-wait-inventory.md).

## Scope and measurement

This audit uses retained physical Agent runs from
`test_agent/run/agent_run_journal.v1.sqlite3` and the corresponding structured
Limited Graph results in `test_agent/run/agent_sessions.sqlite3`. It compares
only outcomes whose final answer confirms that the requested corner motion,
both slicing submissions, and the intermediate point-above-first-slice motion
completed. Runs that merely reached the Agent's `COMPLETED` journal state but
denied motion, failed IK, omitted cutting, or used the wrong retract point are
excluded.

Agent wall time is `terminal_at - started_at`. Top-level tool occupied time is
the sum of matching `tool.called` to `tool.completed` journal intervals. The
orchestration residual is wall time minus those intervals; it includes model
deliberation, streamed response processing, and gaps between tool calls, so it
is not a pure model-latency measurement. Graph active time comes from the
retained `active_runtime_ms` field and excludes Agent deliberation before and
after the graph call.

The first retained `run_limited_graph` call occurred at
`2026-08-16T09:56:16.578306Z`. Runs before that timestamp are treated as the
no-graph population. This is a runtime cutoff, not the later Git commit time.

## True no-graph comparison

There is no successful one-prompt no-graph baseline for the complete workflow.
The two retained compound attempts immediately before Limited Graph,
`c02f6fc0-f911-4568-891d-759c7cb727b1` and
`95ce28be-1630-4638-9719-ecb2c8a208cf`, both stopped before either requested
slice. A strict successful single-prompt no-graph completion time therefore
does not exist.

The closest semantically valid no-graph baseline is one successful workflow
that the operator decomposed into four prompts in the same session:

- `21b593f1-57a0-4a98-a7fa-6cc8bbf57067`: scene mapping, 34.017 seconds;
- `b58a8803-e132-4a75-9565-17e2904a8f64`: corner-relative motion, 13.305
  seconds;
- `d546b104-490c-455f-b79f-8215776202b1`: first slice, 22.253 seconds; and
- `b7cac2a6-a162-4077-bcd4-e04f7a766d8a`: point-above-first-slice motion and
  second slice, 34.217 seconds.

Those four Agent-active intervals sum to 103.792 seconds. From the first
prompt's start to the last answer, observed operator workflow time was 132.119
seconds, including 28.327 seconds between runs. The sequence used 11 top-level
tool calls. Direct finite-Skill calls occupied 34.023 seconds; lifecycle and
policy tools are excluded from that Skill-only number.

The current comparison run is
`2792e656-a7cc-4f26-806b-789c03b5ded3`: 91.225 seconds, four top-level tool
calls, and one graph with eight Skill nodes plus terminal nodes and 34.125
seconds of active runtime.

| Measurement | No graph, four prompts | Current graph, one prompt | Change |
| --- | ---: | ---: | ---: |
| Summed Agent-active time | 103.792 s | 91.225 s | −12.567 s, −12.1%, 1.14× faster |
| Observed operator workflow time | 132.119 s | 91.225 s | −40.895 s, −31.0%, 1.45× faster, including operator gaps |
| Agent orchestration residual | 47.785 s | 40.606 s | −7.179 s, −15.0%, 1.18× faster |
| Direct Skill/graph-child time | 34.023 s | 34.125 s | +0.102 s, +0.3%, effectively flat |
| Top-level tool calls | 11 | 4 | 7 fewer calls |

The defensible machine-active speedup from no graph to the current graph is
therefore about 1.14×, not a large acceleration. The larger 1.45× operator-
workflow figure includes the human gaps required by four separate prompts and
must not be presented as pure Agent acceleration. The main before/after change
is that the complete objective can now finish from one prompt; the retained
no-graph compound attempts did not finish it.

## Early multi-graph to current single graph

The two fully successful early-graph runs are:

- `f70f31db-0dda-4954-86b6-07332fdf2621`: 101.140 seconds, eight top-level
  tool calls, and 32.751 seconds in the two successful physical graph calls.
- `0eb0141c-9ea5-42af-8647-8623079977d4`: 105.243 seconds, seven top-level
  tool calls, and 34.181 seconds in the two successful physical graph calls.

| Measurement | Early-graph mean | Current graph | Change |
| --- | ---: | ---: | ---: |
| Agent end-to-end wall time | 103.192 s | 91.225 s | −11.967 s, −11.6%, 1.13× faster |
| Agent orchestration residual | 51.436 s | 40.606 s | −10.830 s, −21.1%, 1.27× faster |
| Top-level tool occupied time | 51.756 s | 50.619 s | −1.137 s, −2.2%, effectively flat |
| Successful graph active time | 33.466 s | 34.125 s | +0.659 s, +2.0%, effectively flat |
| Top-level tool calls | 7.5 mean | 4 | 3–4 fewer calls |

This separate 1.13× comparison measures consolidation from multiple early
graph submissions into one complete graph. It is not a before-Skill-Graph
versus after-Skill-Graph comparison. Its gain is also Agent orchestration;
physical motion and cutting time did not become materially faster.

The immediately preceding correct current-system run,
`5eefde4d-20b7-4cc1-b327-463cd1abec87`, took 82.890 seconds and its graph took
35.562 seconds. The newest Agent run was 10.1% slower than that single run even
though its graph was 4.0% faster. This variation is why the result is reported
as an estimate rather than a deterministic benchmark.

## Coordinate mini-Skills

In the newest graph, the coordinate-only children occupied:

- `derive_fabric_world_point`: 140 ms;
- `translate_fabric_direction_to_world`: 31 ms; and
- `offset_world_point`: 32 ms.

Together they used 203 ms, or 0.6% of the 34.125-second graph. In the preceding
correct run the same three operations used 125 ms. The combined coordinate
time therefore did not improve; the 78 ms difference is normal scene/Fabric
snapshot variation at this scale. The new offset child itself changed from 47
ms to 32 ms, a 15 ms reduction that is too small to explain end-to-end speed.
Its performance benefit is structural: it replaces a new model turn or unsafe
model arithmetic with a deterministic, Fabric-owned operation.

In the four-prompt no-graph baseline, the published Fabric point-derivation and
direction-translation calls used 277 ms in total; the point-above-first-slice
arithmetic occurred in Agent reasoning because no offset Skill existed. The
current three coordinate children use 203 ms. That apparent 74 ms reduction is
operationally negligible and too sensitive to Fabric snapshot timing to claim
as a general mini-Skill acceleration.

## Calibration-chain cross-check

The identical calibration-and-motion prompt changed from 163.490 seconds in
run `9386c96e-14e4-4be6-87d6-382889a57298` to 140.371 seconds in run
`92e8772a-5edb-4f4a-86f7-1dce79d6f401`, a reduction of 23.119 seconds or
14.1% and an observed 1.16× speedup. Its two graph calls changed from 136.531
seconds to 114.406 seconds. Of the 22.125-second graph reduction, 21.703
seconds came from FoundationPose candidate production. This cross-check must
not be attributed to Limited Graph acceleration because FoundationPose runtime
and perception conditions dominate that difference.

## 2026-08-18 compact-context live checkpoint

Run `b30f99cd-2967-42de-9290-77bf9f5c7022` is the first retained successful
apples-to-apples two-cut workflow after the compact Manager catalog, two-tier
Skill results, combined policy/runtime observation, concise graph authoring,
and initial-binding compatibility repairs were all active. It completed in
75.582 seconds. The three top-level tools occupied 50.009 seconds, leaving
25.573 seconds of Agent orchestration residual. Its eight-Skill graph completed
in 33.828 seconds with eight transitions, four physical actions, no retry, no
model route, no limit, and no failure.

Against the prior current-system run `2792e656-a7cc-4f26-806b-789c03b5ded3`,
Agent orchestration decreased from 40.606 to 25.573 seconds: 15.033 seconds or
37.0% less, an observed 1.59x speedup. End-to-end wall decreased from 91.225 to
75.582 seconds: 15.643 seconds or 17.1% less, an observed 1.21x speedup.
Top-level tool time changed from 50.619 to 50.009 seconds and graph active time
from 34.125 to 33.828 seconds; both are effectively flat.

The five Agent response intervals in the new run were 3.132 seconds from run
start to the combined policy/runtime call, 2.723 seconds from that result to
the Provider call, 3.475 seconds from Provider readiness to deferred Skill
discovery, 11.208 seconds from discovery output to graph submission, and 5.009
seconds from graph completion to the final answer. The graph-construction span
from Provider readiness through submission was 14.695 seconds, down from
22.706 seconds in the prior run. The first live run still needed a deferred
Skill-discovery response, so response count remained five even though setup
policy and runtime observation were combined.

The compact payload changes explain the Agent-side reduction. The regulated
runtime payload was 13,140 characters rather than 101,700, the graph argument
was 4,161 rather than 8,239, and the compact graph result was 14,581 rather
than 128,485. Using the same retained 32-item serialized-history measurement,
the graph-generation input footprint decreased from 310,889 to 139,834
characters and the final-response footprint from 456,024 to 165,184. These are
serialized character counts, not token-usage records.

Immediate repeat run `6724f1b6-5fb3-413e-b680-a7d68798ec75` authored the same
graph digest without another deferred discovery call. Its Provider-ready-to-
graph interval was 8.483 seconds. The complete request also included a separate
Basic safe-home operation, so its 20.786-second Agent residual is not directly
apples-to-apples with the no-safe-home baseline. It nevertheless completed the
graph, the additional safe-home call, and the final response in 65.391 seconds.

Both live graphs emitted their SAM2 visual evidence 0.484 and 0.344 seconds
after graph submission, respectively, more than 33 seconds before graph
completion. This verifies incremental graph visual publication for these
single-evidence runs. It does not test multiple simultaneous evidence cards.
Both graphs exercised successful child-declared Integrated Provider handover.
Neither exercised a retry, failure branch, switch, or model-routing branch.

## Conclusion

For the clean, comparable slicing workflow, the corrected estimates are:

- strict single-prompt no graph to current graph: no speed ratio is available
  because neither retained no-graph compound attempt completed;
- equivalent no-graph work split across four prompts: 103.8 seconds of Agent
  activity to 91.2 seconds, approximately 1.14× faster;
- observed operator workflow including inter-prompt gaps: 132.1 seconds to
  91.2 seconds, approximately 1.45× faster;
- early multi-graph to current single graph: 103.2 seconds to 91.2 seconds,
  approximately 1.13× faster;
- direct Skill/graph-child time: 34.0 seconds to 34.1 seconds, effectively
  unchanged; and
- coordinate mini-Skills: about 0.2 seconds total, operationally negligible.

Limited Graph's demonstrated improvement is one-prompt completion, reliability,
and reduced operator/Agent handoffs first. Machine-active latency improved only
modestly because Provider readiness, perception, physical motion, and contact
execution still dominate the workflow.
