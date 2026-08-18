# Slicing alignment preview rejection handoff

## Affected runs

- `2a3a6f77-ecfe-464e-afbe-887b004a33e6`, started
  `2026-08-18T04:06:51.462892Z`.
- `9d3a86e6-86de-4e6c-a9ad-06e206d937d2`, started
  `2026-08-18T04:17:51.937723Z`.
- `2869b95b-a158-42fa-aa39-6e0c7056d8c0`, started
  `2026-08-18T05:39:48.650035Z`.
- `b00c5c6b-3d56-44ff-b2a6-2d31c5ebbcc2`, started
  `2026-08-18T05:41:19.462097Z`.
- `04ff2e46-5024-4ef0-bca0-734a39da19e5`, started
  `2026-08-18T06:08:20.063629Z`.
- `e54aaecf-4b93-42b8-9f78-64559a87a73b`, started
  `2026-08-18T06:09:32.695975Z`.

## Requested slicing operation

- Begin point: current IK location plus world `[0, 0, -0.10] m`.
- Blade direction: world `[0, 0, -1]`.
- Slicing direction: arm-base `[-1, 0, 0]`, translated to world before the
  slicing call.
- Slice length: `0.20 m`.
- Blade and motion profile selectors: `null`, resolving the live defaults.
- Integrated alignment backend: `IMPEDANCE`.

## First run error

```text
RuntimeError: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: candidate approaches a singularity (sigma 0.00000); IK position residual 0.005602 m exceeds 0.001500 m; IK orientation residual 0.145810 rad exceeds 0.035000 rad
```

## Second run error

```text
RuntimeError: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: candidate approaches a singularity (sigma 0.00000); IK position residual 0.007761 m exceeds 0.001500 m; IK orientation residual 0.199557 rad exceeds 0.035000 rad
```

## Recorded execution state

- Both invocations reached the first `slice_with_blade` graph node.
- Neither invocation produced a retained Slicing result.
- Neither invocation executed a Contact result.
- Neither invocation reached the offset-above-first-point node, the following
  free-space move, or the second slice.
- The graph supplied the same first-slice semantic inputs as retained successful
  canonical graph message `9811`.

## Third run graph error

```text
SlicingAlignmentPreviewRejected: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: SHADOW_PLANNING_TIME_BUDGET_EXCEEDED
```

## Third run direct retry error

```text
SlicingAlignmentPreviewRejected: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: SHADOW_PLANNING_TIME_BUDGET_EXCEEDED
```

## Fourth run error

```text
SlicingAlignmentPreviewRejected: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: candidate approaches a singularity (sigma 0.00000); IK position residual 0.008743 m exceeds 0.001500 m; IK orientation residual 0.223050 rad exceeds 0.035000 rad
```

## Fifth run error

```text
SlicingAlignmentPreviewRejected: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: candidate approaches a singularity (sigma 0.00000); IK position residual 0.009289 m exceeds 0.001500 m; IK orientation residual 0.235562 rad exceeds 0.035000 rad
```

## Sixth run error

```text
SlicingAlignmentPreviewRejected: Integrated did not produce a slicing alignment preview: IK_PREVIEW_REJECTED; Integrated Controller rejected the requested path preview: SHADOW_PLANNING_TIME_BUDGET_EXCEEDED
```
