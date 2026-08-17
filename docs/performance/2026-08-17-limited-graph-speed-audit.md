# Limited Graph speed audit

Date: 2026-08-17

## Scope and measurement

This audit uses retained physical Agent runs from
`test_agent/run/agent_run_journal.v1.sqlite3` and the corresponding structured
Limited Graph results in `test_agent/run/agent_sessions.sqlite3`. It compares
only runs whose final answer confirms that the requested corner motion, both
slicing submissions, and the intermediate point-above-first-slice motion
completed. Runs that merely reached the Agent's `COMPLETED` journal state but
denied motion, failed IK, omitted cutting, or used the wrong retract point are
excluded.

The two historical pre-change baselines are:

- `f70f31db-0dda-4954-86b6-07332fdf2621`: 101.140 seconds, eight top-level
  tool calls, and 32.751 seconds in the two successful physical graph calls.
- `0eb0141c-9ea5-42af-8647-8623079977d4`: 105.243 seconds, seven top-level
  tool calls, and 34.181 seconds in the two successful physical graph calls.

The post-change run is
`2792e656-a7cc-4f26-806b-789c03b5ded3`: 91.225 seconds, four top-level tool
calls, and one eight-node graph with 34.125 seconds of active runtime.

Agent wall time is `terminal_at - started_at`. Top-level tool occupied time is
the sum of matching `tool.called` to `tool.completed` journal intervals. The
orchestration residual is wall time minus those intervals; it includes model
deliberation, streamed response processing, and gaps between tool calls, so it
is not a pure model-latency measurement. Graph active time comes from the
retained `active_runtime_ms` field and excludes Agent deliberation before and
after the graph call.

## Comparable two-slice result

| Measurement | Historical mean | Latest | Change |
| --- | ---: | ---: | ---: |
| Agent end-to-end wall time | 103.192 s | 91.225 s | −11.967 s, −11.6%, 1.13× faster |
| Agent orchestration residual | 51.436 s | 40.606 s | −10.830 s, −21.1%, 1.27× faster |
| Top-level tool occupied time | 51.756 s | 50.619 s | −1.137 s, −2.2%, effectively flat |
| Successful graph active time | 33.466 s | 34.125 s | +0.659 s, +2.0%, effectively flat |
| Top-level tool calls | 7.5 mean | 4 | 3–4 fewer calls |

The measured speedup is therefore an Agent-orchestration improvement. One
complete graph replaced multiple model decisions, Provider setup calls, and
separate graph submissions. Physical motion and cutting time did not become
materially faster.

The immediately preceding correct post-change run,
`5eefde4d-20b7-4cc1-b327-463cd1abec87`, took 82.890 seconds and its graph took
35.562 seconds. The newest Agent run was 10.1% slower than that single run even
though its graph was 4.0% faster. This variation is why the historical result
is reported as an estimate rather than a deterministic benchmark.

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

## Calibration-chain cross-check

The identical calibration-and-motion prompt changed from 163.490 seconds in
run `9386c96e-14e4-4be6-87d6-382889a57298` to 140.371 seconds in run
`92e8772a-5edb-4f4a-86f7-1dce79d6f401`, a reduction of 23.119 seconds or
14.1% and an observed 1.16× speedup. Its two graph calls changed from 136.531
seconds to 114.406 seconds. Of the 22.125-second graph reduction, 21.703
seconds came from FoundationPose candidate production. This cross-check must
not be attributed to Limited Graph acceleration because FoundationPose runtime
and perception conditions dominate that difference.

## Conclusion

For the clean, comparable slicing workflow, the defensible estimate is:

- Agent time: 103.2 seconds to 91.2 seconds, approximately 1.13× faster;
- graph/Skill time: 33.5 seconds to 34.1 seconds, effectively unchanged; and
- coordinate mini-Skills: about 0.2 seconds total, operationally negligible.

The improvement is reliability and fewer Agent handoffs first, with a modest
end-to-end latency benefit as a consequence.
